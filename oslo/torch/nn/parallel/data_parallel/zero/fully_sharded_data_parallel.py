# Copyright 2021 HPC-AI Technology Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Modified by EleutherAI on 2023.

import itertools
from collections import OrderedDict
from functools import partial
from typing import Dict, List, Optional

import torch
import torch.distributed as dist
import torch.nn as nn
from oslo.torch.utils.logging import DistributedLogger

from oslo.torch.distributed.parallel_context import ParallelContext
from oslo.torch.distributed.parallel_mode import ParallelMode
from oslo.torch.nn.parallel.data_parallel._utils import is_ddp_ignored
from oslo.torch.nn.parallel.data_parallel.data_parallel import _DistributedDataParallel
from oslo.torch.nn.parallel.data_parallel.zero.chunk import (
    Chunk,
    ChunkManager,
    TensorState,
)
from oslo.torch.nn.parallel.data_parallel.zero.heterogeneous_manager import (
    HeterogeneousMemoryManager,
)
from oslo.torch.nn.parallel.data_parallel.zero.memory_tracer.param_runtime_order import (
    OrderedParamGenerator,
)
from oslo.torch.nn.parallel.data_parallel.zero.utils import (
    get_current_device,
    get_temp_total_chunk_on_cuda,
)
from oslo.torch.nn.parallel.data_parallel.zero.heterogeneous_hook import (
    HeterogeneousZeROHook,
)

from oslo.torch.nn.parallel.data_parallel.zero.memory_tracer import (
    MemStats,
)
from oslo.torch.nn.parallel.data_parallel.zero.chunk import (
    init_chunk_manager,
)

try:
    from torch.nn.modules.module import _EXTRA_STATE_KEY_SUFFIX, _IncompatibleKeys
except ImportError:
    _EXTRA_STATE_KEY_SUFFIX = "_extra_state"


def _cast_float(args, dtype: torch.dtype):
    if isinstance(args, torch.Tensor) and torch.is_floating_point(args):
        args = args.to(dtype)
    elif isinstance(args, (list, tuple)):
        args = type(args)(_cast_float(t, dtype) for t in args)
    elif isinstance(args, dict):
        args = {k: _cast_float(v, dtype) for k, v in args.items()}
    return args


class _FullyShardedDataParallel(_DistributedDataParallel):
    """Fully sharded data parallel.
    Warning: Nested FullyShardedDataParallel is not supported now.
    It is designed to be used with ChunkManager and HeterogeneousMemoryManager.
    For more details, see the API reference of ``ChunkManager`` and ``HeterogeneousMemoryManager``.

    Args:
        module (torch.nn.Module): Module to apply ZeRO-DP.
        device (torch.device): Device to place the module.
        parallel_context (ParallelContext): process group object.
        placement_policy (str): Placement policy for the chunks.
        pin_memory (bool): Chunks on CPU Memory use pin-memory.
        force_outputs_fp32 (bool): If set to True, outputs will be fp32. Otherwise, outputs will be fp16.
            Defaults to False.
        search_range_mb (int): Search range for the chunk size. Defaults to 32.
        hidden_dim (int): Hidden dimension for the chunk size search. Defaults to None.
        min_chunk_size_mb (int): Minimum chunk size in MB. Defaults to 32.
        memstats (MemStats): Memory statistics. Defaults to None.
    """

    def __init__(
        self,
        module: torch.nn.Module,
        device: torch.device,
        parallel_context: ParallelContext = None,
        placement_policy: str = "cuda",
        pin_memory: bool = False,
        force_outputs_fp32: bool = True,
        search_range_mb: int = 32,
        hidden_dim: Optional[int] = None,
        min_chunk_size_mb: float = 32,
        memstats: Optional[MemStats] = None,
    ) -> None:
        super().__init__(module, parallel_context=parallel_context)
        self.chunk_manager: ChunkManager = init_chunk_manager(
            model=module,
            init_device=device,
            hidden_dim=hidden_dim,
            search_range_mb=search_range_mb,
            min_chunk_size_mb=min_chunk_size_mb,
        )
        self.heterogeneous_manager = HeterogeneousMemoryManager(
            placement_policy, self.chunk_manager, memstats
        )
        self.force_outputs_fp32 = force_outputs_fp32
        self.param_op_hook = HeterogeneousZeROHook(self.heterogeneous_manager)
        self.fp32_params: List[torch.Tensor] = list()
        self.fp16_params: List[torch.Tensor] = list()
        self.overflow_counter = 0
        self.grads_device: Dict[torch.Tensor, torch.device] = dict()
        self.param2name: Dict[nn.Parameter, str] = dict()
        self.name2param: Dict[str, nn.Parameter] = dict()

        self._cast_buffers()
        self._logger = DistributedLogger.get_instance(__name__)

        if self.heterogeneous_manager._premade_memstats_:
            # build chunk in param runtime visited order.
            param_order = self.heterogeneous_manager.memstats()._param_runtime_order
        else:
            # build chunk in param initialized order.
            # Note: in this way, it can not get filter unused params during runtime.
            param_order = OrderedParamGenerator()
            for p in module.parameters():
                param_order.append(p)

        self._init_chunks(
            param_order=param_order,
            cpu_offload=self.heterogeneous_manager.policy_name != "cuda",
            pin_memory=pin_memory,
        )

        for name, param in module.named_parameters():
            self.param2name[param] = name
        for m_name, m_var in module.named_modules():
            for p_name, p_var in m_var.named_parameters(recurse=False):
                param_name = m_name + "." + p_name if m_name else p_name
                self.name2param[param_name] = p_var

    def _post_forward(self):
        """This function is only triggered for inference."""
        access_list = list(self.chunk_manager.accessed_chunks)
        # we need to scatter all accessed chunks and move them to their original places
        for chunk in access_list:
            if chunk.keep_gathered:
                self.chunk_manager.fake_release_chunk(chunk)
            else:
                assert chunk.can_release
                self.chunk_manager.release_chunk(chunk)
            first_param = next(iter(chunk.tensors_info))
            self.chunk_manager.move_chunk(chunk, self.grads_device[first_param])
        assert self.chunk_manager.accessed_mem == 0
        # reset all recorded attributes
        self.heterogeneous_manager.reset_attributes()

    def forward(self, *args, **kwargs):
        # check whether we are in a inference mode
        grad_flag = torch.is_grad_enabled()
        if not grad_flag:
            assert (
                not self.heterogeneous_manager.need_warmup
                or not self.heterogeneous_manager.is_warmup()
            ), "You should run a completed iteration as your warmup iter"

        args, kwargs = _cast_float(args, torch.half), _cast_float(kwargs, torch.half)

        self.heterogeneous_manager.pre_iter(*args)
        self.param_op_hook.pre_forward(self.fp16_params)
        outputs = super().forward(*args, **kwargs)
        self.param_op_hook.post_forward(self.fp16_params)
        # scatter chunks in the inference mode
        if not grad_flag:
            self._post_forward()

        if self.force_outputs_fp32:
            return _cast_float(outputs, torch.float)
        return outputs

    def _setup_grads_ptr(self):
        for p in self.module.parameters():
            if is_ddp_ignored(p):
                continue
            p.grad = None

    def _pre_backward(self):
        # set the context as backward
        self.param_op_hook.toggle_training_phase()

        # set a visit label for all parameters
        # the label is used to check whether the parameter is correctly reduced
        for param in self.param2name:
            if not is_ddp_ignored(param):
                setattr(param, "_heterogeneous_reduced", False)

        self.param_op_hook.pre_backward(self.fp16_params)

    def _post_backward(self):
        # reset the context for forward
        self.param_op_hook.toggle_training_phase()

        if self.chunk_manager.accessed_mem != 0:
            error_params = ["Reduction failed at followed parameters:"]
            for param in self.param2name:
                if not is_ddp_ignored(param) and not getattr(
                    param, "_heterogeneous_reduced"
                ):
                    error_params.append(self.param2name[param])
            error_str = "\n\t".join(error_params)
            raise RuntimeError(
                "ZERO DDP error: the synchronization of gradients doesn't exit properly.",
                "The most possible reason is that the model is not compatible with ZeroDDP.\n",
                f"{error_str}",
            )
        self._setup_grads_ptr()
        self._logger.debug(
            f"comp cuda demand time: {self.heterogeneous_manager._comp_cuda_demand_time}, layout time: {self.heterogeneous_manager._layout_time}, evict time: {self.heterogeneous_manager._evict_time}, CPU->CUDA vol: {self.heterogeneous_manager._h2d_volume}B, CUDA->CPU vol: {self.heterogeneous_manager._d2h_volume}"
        )
        self.heterogeneous_manager.post_iter()

    def grad_handle(self, p, grad):
        self.param_op_hook.post_backward([p])
        empty_grad = torch.empty_like(grad)

        chunk = self.chunk_manager.get_chunk(p)
        if chunk.tensors_info[p].state != TensorState.HOLD_AFTER_BWD:
            raise RuntimeError(
                f"Parameter `{self.param2name[p]}` failed at the gradient reduction. "
                "Some unsupported torch function is operated upon this parameter."
            )
        self.chunk_manager.trans_tensor_state(p, TensorState.READY_FOR_REDUCE)
        chunk.copy_tensor_to_chunk_slice(p, grad)
        reduced = self.chunk_manager.reduce_chunk(chunk)
        if reduced:
            if chunk.is_gathered:
                chunk.cuda_global_chunk.div_(chunk.pg_size)
            else:
                chunk.cuda_shard.div_(chunk.pg_size)
            # check overflow elements
            self.overflow_counter += chunk.has_inf_or_nan
            # record l2 norm for gradient clipping
            if chunk.l2_norm_flag:
                chunk.set_l2_norm()
            self.chunk_manager.move_chunk(chunk, self.grads_device[p], force_copy=True)
        return empty_grad

    def zero_grad(self, set_to_none: bool = False) -> None:
        self.module.zero_grad(set_to_none=True)

    def set_chunk_grad_device(self, chunk: Chunk, device: torch.device) -> None:
        for tensor in chunk.get_tensors():
            self.grads_device[tensor] = device

    def state_dict(
        self, destination=None, prefix="", keep_vars=False, only_rank_0: bool = True
    ):
        """Returns a dictionary containing a whole state of the module.

        Both parameters and persistent buffers (e.g. running averages) are included.
        Keys are corresponding parameter and buffer names.
        Parameters and buffers set to ``None`` are not included.

        Warning: The non strict state dict would ignore the parameters if the tensors of the parameters
            are shared with other parameters which have been included in the dictionary.
            When you need to load the state dict, you should set the argument `strict` to False.

        Returns:
            dict:
                a dictionary containing a whole state of the module
        """
        if destination is None:
            destination = OrderedDict()
            destination._metadata = OrderedDict()
        destination._metadata[prefix[:-1]] = local_metadata = dict(
            version=self._version
        )
        self._save_to_state_dict(destination, prefix, keep_vars, only_rank_0)

        for hook in self._state_dict_hooks.values():
            hook_result = hook(self, destination, prefix, local_metadata)
            if hook_result is not None:
                destination = hook_result
        return destination

    def _get_param_to_save_data(
        self, param_list: List[torch.nn.Parameter], only_rank_0: bool
    ) -> Dict:
        """
        get param content from chunks.

        Args:
            param_list (_type_): a list of torch.nn.Parameters
            only_rank_0 (_type_): _description_

        Returns:
            Dict: a dict whose key is param name and value is param with correct payload
        """
        # save parameters
        param_to_save_data = dict()
        chunk_list = self.chunk_manager.get_chunks(param_list)
        for chunk in chunk_list:
            temp_chunk = get_temp_total_chunk_on_cuda(chunk)

            for tensor, tensor_info in chunk.tensors_info.items():
                record_tensor = torch.empty([0])
                record_flag = (not only_rank_0) | (dist.get_rank(chunk.torch_pg) == 0)
                if record_flag:
                    record_tensor = (
                        temp_chunk[tensor_info.offset : tensor_info.end]
                        .view(tensor.shape)
                        .cpu()
                    )

                assert tensor not in param_to_save_data
                param_to_save_data[tensor] = record_tensor

            del temp_chunk
        return param_to_save_data

    def _save_to_state_dict(self, destination, prefix, keep_vars, only_rank_0=True):
        r"""Saves module state to `destination` dictionary, containing a state
        of the module, but not its descendants. This is called on every
        submodule in :meth:`~torch.nn.Module.state_dict`.

        In rare cases, subclasses can achieve class-specific behavior by
        overriding this method with custom logic.

        Args:
            destination (dict): a dict where state will be stored
            prefix (str): the prefix for parameters and buffers used in this
                module
        """
        assert (
            keep_vars is False
        ), "`state_dict` with parameter, `keep_vars=True`, is not supported now."

        # get copies of fp32 parameters in CPU
        param_to_save_data = self._get_param_to_save_data(self.fp32_params, only_rank_0)
        # get the mapping between copies and fp16 parameters
        p_mapping = dict()
        for p, fp32_p in zip(self.fp16_params, self.fp32_params):
            name = self.param2name[p]
            assert (
                fp32_p in param_to_save_data
            ), "Parameter '{}' is neglected in the chunk list".format(name)
            record_parameter = param_to_save_data[fp32_p]
            p_mapping[p] = record_parameter
        for name, param in self.name2param.items():
            if param is not None:
                if is_ddp_ignored(param):
                    # deal with ddp ignored parameters
                    destination[prefix + name] = param if keep_vars else param.detach()
                else:
                    destination[prefix + name] = p_mapping[param]
        del p_mapping
        del param_to_save_data

        # save all buffers
        for name, buf in self.named_buffers():
            if buf is not None and name not in self._non_persistent_buffers_set:
                destination[prefix + name] = buf if keep_vars else buf.detach()
        # save extra states
        extra_state_key = prefix + _EXTRA_STATE_KEY_SUFFIX
        if (
            getattr(self.__class__, "get_extra_state", torch.nn.Module.get_extra_state)
            is not torch.nn.Module.get_extra_state
        ):
            destination[extra_state_key] = self.get_extra_state()

    def load_state_dict(
        self, state_dict: "OrderedDict[str, torch.Tensor]", strict: bool = True
    ):
        r"""Copies parameters and buffers from :attr:`state_dict` into
        this module and its descendants. If :attr:`strict` is ``True``, then
        the keys of :attr:`state_dict` must exactly match the keys returned
        by this module's :meth:`~torch.nn.Module.state_dict` function.

        Args:
            state_dict (dict): a dict containing parameters and
                persistent buffers.
            strict (bool, optional): whether to strictly enforce that the keys
                in :attr:`state_dict` match the keys returned by this module's
                :meth:`~torch.nn.Module.state_dict` function. Default: ``True``

        Returns:
            ``NamedTuple`` with ``missing_keys`` and ``unexpected_keys`` fields:
                * **missing_keys** is a list of str containing the missing keys
                * **unexpected_keys** is a list of str containing the unexpected keys

        Note:
            If a parameter or buffer is registered as ``None`` and its corresponding key
            exists in :attr:`state_dict`, :meth:`load_state_dict` will raise a
            ``RuntimeError``.
        """
        missing_keys: List[str] = []
        unexpected_keys: List[str] = []
        error_msgs: List[str] = []

        # copy state_dict so _load_from_state_dict can modify it
        metadata = getattr(state_dict, "_metadata", None)
        state_dict = state_dict.copy()
        if metadata is not None:
            # mypy isn't aware that "_metadata" exists in state_dict
            state_dict._metadata = metadata  # type: ignore[attr-defined]

        prefix = ""
        local_metadata = {} if metadata is None else metadata.get(prefix[:-1], {})
        self._load_from_state_dict(
            state_dict,
            prefix,
            local_metadata,
            True,
            missing_keys,
            unexpected_keys,
            error_msgs,
        )

        if strict:
            if len(unexpected_keys) > 0:
                error_msgs.insert(
                    0,
                    "Unexpected key(s) in state_dict: {}. ".format(
                        ", ".join('"{}"'.format(k) for k in unexpected_keys)
                    ),
                )
            if len(missing_keys) > 0:
                error_msgs.insert(
                    0,
                    "Missing key(s) in state_dict: {}. ".format(
                        ", ".join('"{}"'.format(k) for k in missing_keys)
                    ),
                )

        if len(error_msgs) > 0:
            raise RuntimeError(
                "Error(s) in loading state_dict for {}:\n\t{}".format(
                    self.__class__.__name__, "\n\t".join(error_msgs)
                )
            )
        return _IncompatibleKeys(missing_keys, unexpected_keys)

    def _load_from_state_dict(
        self,
        state_dict,
        prefix,
        local_metadata,
        strict,
        missing_keys,
        unexpected_keys,
        error_msgs,
    ):
        r"""Copies parameters and buffers from :attr:`state_dict` into only
        this module, but not its descendants. This is called on every submodule
        in :meth:`~torch.nn.Module.load_state_dict`. Metadata saved for this
        module in input :attr:`state_dict` is provided as :attr:`local_metadata`.
        For state dicts without metadata, :attr:`local_metadata` is empty.
        Subclasses can achieve class-specific backward compatible loading using
        the version number at `local_metadata.get("version", None)`.

        .. note::
            :attr:`state_dict` is not the same object as the input
            :attr:`state_dict` to :meth:`~torch.nn.Module.load_state_dict`. So
            it can be modified.

        Args:
            state_dict (dict): a dict containing parameters and
                persistent buffers.
            prefix (str): the prefix for parameters and buffers used in this
                module
            local_metadata (dict): a dict containing the metadata for this module.
                See
            strict (bool): whether to strictly enforce that the keys in
                :attr:`state_dict` with :attr:`prefix` match the names of
                parameters and buffers in this module
            missing_keys (list of str): if ``strict=True``, add missing keys to
                this list
            unexpected_keys (list of str): if ``strict=True``, add unexpected
                keys to this list
            error_msgs (list of str): error messages should be added to this
                list, and will be reported together in
                :meth:`~torch.nn.Module.load_state_dict`
        """
        for hook in self._load_state_dict_pre_hooks.values():
            hook(
                state_dict,
                prefix,
                local_metadata,
                strict,
                missing_keys,
                unexpected_keys,
                error_msgs,
            )

        persistent_buffers = {
            k: v
            for k, v in self.named_buffers()
            if k not in self._non_persistent_buffers_set
        }
        local_name_params = itertools.chain(
            self.named_parameters(), persistent_buffers.items()
        )
        local_state = {k: v for k, v in local_name_params if v is not None}

        def load(param_name, dest_tensor, copy_func):
            state_key = prefix + param_name
            if state_key in state_dict:
                input_param = state_dict[state_key]
                # Backward compatibility: loading 1-dim tensor from 0.3.* to version 0.4+
                if len(dest_tensor.shape) == 0 and len(input_param.shape) == 1:
                    input_param = input_param[0]
                if input_param.shape != dest_tensor.shape:
                    # local shape should match the one in checkpoint
                    error_msgs.append(
                        "size mismatch for {}: copying a param with shape {} from checkpoint, "
                        "the shape in current model is {}.".format(
                            state_key, input_param.shape, dest_tensor.shape
                        )
                    )
                    return
                try:
                    with torch.no_grad():
                        copy_func(input_param)
                except Exception as ex:
                    error_msgs.append(
                        'While copying the parameter named "{}", '
                        "whose dimensions in the model are {} and "
                        "whose dimensions in the checkpoint are {}, "
                        "an exception occurred : {}.".format(
                            state_key, dest_tensor.size(), input_param.size(), ex.args
                        )
                    )
            elif strict:
                missing_keys.append(state_key)

        def load_fp32_parameter(chunk_slice, data):
            chunk_slice.copy_(data.flatten())

        for name, param in self.named_parameters():
            if is_ddp_ignored(param):
                # deal with ddp ignored parameters
                load(name, param, param.copy_)

        fp32_to_name = dict()
        for p, fp32_p in zip(self.fp16_params, self.fp32_params):
            if p is not None:
                name = self.param2name[p]
                fp32_to_name[fp32_p] = name

        chunk_list = self.chunk_manager.get_chunks(self.fp32_params)
        for chunk in chunk_list:
            temp_chunk = get_temp_total_chunk_on_cuda(chunk)

            for tensor, tensor_info in chunk.tensors_info.items():
                parameter_name = fp32_to_name[tensor]
                parameter_slice = temp_chunk[tensor_info.offset : tensor_info.end]
                load(
                    parameter_name,
                    tensor,
                    partial(load_fp32_parameter, parameter_slice),
                )

            if chunk.is_gathered:
                chunk.cuda_global_chunk.copy_(temp_chunk)
            elif chunk.cuda_shard is not None:
                chunk.cuda_shard.copy_(temp_chunk[chunk.shard_begin : chunk.shard_end])
            else:
                chunk.cpu_shard.copy_(temp_chunk[chunk.shard_begin : chunk.shard_end])

            del temp_chunk

        for chunk_32 in chunk_list:
            chunk_16 = chunk_32.paired_chunk
            assert chunk_16 is not None
            chunk_16.optim_update()

        for name, buf in persistent_buffers.items():
            if buf is not None:
                load(name, buf, buf.copy_)

        extra_state_key = prefix + _EXTRA_STATE_KEY_SUFFIX
        if (
            getattr(self.__class__, "set_extra_state", torch.nn.Module.set_extra_state)
            is not torch.nn.Module.set_extra_state
        ):
            if extra_state_key in state_dict:
                self.set_extra_state(state_dict[extra_state_key])
            elif strict:
                missing_keys.append(extra_state_key)
        elif strict and (extra_state_key in state_dict):
            unexpected_keys.append(extra_state_key)

        if strict:
            for key in state_dict.keys():
                if key.startswith(prefix) and key != extra_state_key:
                    input_name = key[len(prefix) :]
                    if input_name not in local_state:
                        unexpected_keys.append(key)

    def _init_chunks(self, param_order, cpu_offload: bool, pin_memory: bool):
        for p in param_order.generate():
            # ignore the parameters with no gradient
            if not p.requires_grad:
                self.set_params_to_ignore([p])

            # move ignored parameters to CUDA
            if is_ddp_ignored(p):
                p.data = p.data.to(device=get_current_device(), dtype=torch.float16)
                continue

            # create a fp32 parameter
            fp32_data = p.data.float()
            fp32_p = torch.Tensor(fp32_data)
            # create a fp16 parameter
            p.data = p.data.half()

            # register the fp16 parameter and fp32 parameter in the chunk manager
            dp_world_size = self.parallel_context.get_world_size(ParallelMode.DATA)
            self.chunk_manager.register_tensor(
                tensor=p,
                group_type="fp16_param",
                config_key=dp_world_size,
                parallel_context=self.parallel_context,
                cpu_offload=cpu_offload,
                pin_memory=pin_memory,
            )
            self.chunk_manager.register_tensor(
                tensor=fp32_p,
                group_type="fp32_param",
                config_key=dp_world_size,
                parallel_context=self.parallel_context,
                cpu_offload=cpu_offload,
                pin_memory=pin_memory,
            )

            self.fp16_params.append(p)
            self.fp32_params.append(fp32_p)
            self.grads_device[p] = self.heterogeneous_manager.default_device

        self.chunk_manager.close_all_groups()

        for p, fp32_p in zip(self.fp16_params, self.fp32_params):
            chunk_16 = self.chunk_manager.get_chunk(p)
            chunk_32 = self.chunk_manager.get_chunk(fp32_p)
            chunk_32.init_pair(chunk_16)

            # keep gathered chunks are in CUDA
            if chunk_16.keep_gathered:
                self.grads_device[p] = get_current_device()

    def _cast_buffers(self):
        for buffer in self.module.buffers():
            buffer.data = buffer.cuda()
            if torch.is_floating_point(buffer):
                buffer.data = buffer.half()
