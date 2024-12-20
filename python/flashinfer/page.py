"""
Copyright (c) 2023 by FlashInfer team.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

from typing import Optional

import torch

from .jit import FLASHINFER_CSRC_DIR, has_prebuilt_ops, load_cuda_ops
from .utils import (
    TensorLayout,
    _check_kv_layout,
    _unpack_paged_kv_cache,
    register_custom_op,
    register_fake_op,
)

_page_module = None


def get_page_module():
    global _page_module
    if _page_module is None:
        if has_prebuilt_ops:
            from . import _kernels

            _page_module = _kernels
        else:
            _page_module = load_cuda_ops(
                "page",
                [
                    FLASHINFER_CSRC_DIR / "page.cu",
                    FLASHINFER_CSRC_DIR / "flashinfer_page_ops.cu",
                ],
            )
    return _page_module


@register_custom_op(
    "flashinfer::append_paged_kv_cache",
    mutates_args=("paged_k_cache", "paged_v_cache"),
)
def _append_paged_kv_cache_kernel(
    append_key: torch.Tensor,
    append_value: torch.Tensor,
    append_indptr: torch.Tensor,
    paged_k_cache: Optional[torch.Tensor],
    paged_v_cache: Optional[torch.Tensor],
    kv_indices: torch.Tensor,
    kv_indptr: torch.Tensor,
    kv_last_page_len: torch.Tensor,
    layout: int,
) -> None:
    get_page_module().append_paged_kv_cache(
        append_key,
        append_value,
        append_indptr,
        paged_k_cache,
        paged_v_cache,
        kv_indices,
        kv_indptr,
        kv_last_page_len,
        layout,
    )


@register_fake_op("flashinfer::append_paged_kv_cache")
def _fake_append_paged_kv_cache_kernel(
    append_key: torch.Tensor,
    append_value: torch.Tensor,
    append_indptr: torch.Tensor,
    paged_k_cache: Optional[torch.Tensor],
    paged_v_cache: Optional[torch.Tensor],
    kv_indices: torch.Tensor,
    kv_indptr: torch.Tensor,
    kv_last_page_len: torch.Tensor,
    layout: int,
) -> None:
    pass


def append_paged_kv_cache(
    append_key: torch.Tensor,
    append_value: torch.Tensor,
    append_indptr: torch.Tensor,
    paged_kv_cache: torch.Tensor,
    kv_indices: torch.Tensor,
    kv_indptr: torch.Tensor,
    kv_last_page_len: torch.Tensor,
    kv_layout: str = "NHD",
) -> None:
    r"""Append a batch of key-value pairs to a paged key-value cache.

    Parameters
    ----------
    append_key : torch.Tensor
        The key tensor to append in ragged tensor format, shape:
        ``[append_indptr[-1], num_kv_heads, head_dim]``.
    append_value : torch.Tensor
        The value tensor to append in ragged tensor format, shape:
        ``[append_indptr[-1], num_kv_heads, head_dim]``.
    append_indptr : torch.Tensor
        The indptr tensor of the key-value pairs to append, shape: ``[batch_size + 1]``.
    paged_kv_cache : Union[torch.Tensor, Tuple[torch.Tensor, torch.Tensor]]
        The paged KV-Cache stored as a tuple of tensors or a single tensor:

        * a tuple ``(k_cache, v_cache)`` of 4-D tensors, each with shape:
          ``[max_num_pages, page_size, num_kv_heads, head_dim]`` if :attr:`kv_layout` is ``NHD``,
          and ``[max_num_pages, num_kv_heads, page_size, head_dim]`` if :attr:`kv_layout` is ``HND``.

        * a single 5-D tensor with shape:
          ``[max_num_pages, 2, page_size, num_kv_heads, head_dim]`` if
          :attr:`kv_layout` is ``NHD``, and
          ``[max_num_pages, 2, num_kv_heads, page_size, head_dim]`` if
          :attr:`kv_layout` is ``HND``. Where ``paged_kv_cache[:, 0]`` is the key-cache and
          ``paged_kv_cache[:, 1]`` is the value-cache.

    kv_indices : torch.Tensor
        The page indices of the paged kv-cache, shape: ``[kv_indptr[-1]]``.
    kv_indptr : torch.Tensor
        The indptr of the paged kv-cache, shape: ``[batch_size + 1]``.
    kv_last_page_len : torch.Tensor
        The number of entries in the last page of each request in the paged kv cache,
        shape: ``[batch_size]``.
    kv_layout : str
        The layout of the paged kv-cache, either ``NHD`` or ``HND``.

    Example
    -------
    >>> import torch
    >>> import flashinfer
    >>> nnz_kv = 100
    >>> num_kv_heads = 32
    >>> head_dim = 128
    >>> k_append = torch.randn(nnz_kv, num_kv_heads, head_dim).half().to(0)
    >>> v_append = torch.randn(nnz_kv, num_kv_heads, head_dim).half().to(0)
    >>> # 45 + 8 + 25 + 22 = nnz_kv
    >>> kv_append_length = torch.tensor([45, 8, 25, 22], dtype=torch.int32, device="cuda:0")
    >>> kv_append_indptr = torch.cat(
    ...     [torch.zeros(1).int().to(0), torch.cumsum(kv_append_length, dim=0)]
    ... ).int()
    >>> max_num_pages = 1000
    >>> page_size = 16
    >>> paged_kv_cache = torch.randn(max_num_pages, 2, page_size, num_kv_heads, head_dim).half().to(0)
    >>> num_pages_per_req = torch.tensor([3, 1, 2, 2], dtype=torch.int32, device="cuda:0")
    >>> kv_page_indptr = torch.cat(
    ...     [torch.zeros(1).int().to(0), torch.cumsum(num_pages_per_req, dim=0)]
    ... ).int()
    >>> # use first 8 pages in the paged-kv
    >>> kv_page_indices = torch.arange(8, dtype=torch.int32, device="cuda:0")
    >>> # 45 = (3 - 1) * 16 + 13
    >>> # 8 = (1 - 1) * 16 + 8
    >>> # 25 = (2 - 1) * 16 + 9
    >>> # 22 = (2 - 1) * 16 + 6
    >>> kv_last_page_len = torch.tensor([13, 8, 9, 6], dtype=torch.int32, device="cuda:0")
    >>>
    >>> flashinfer.append_paged_kv_cache(
    ...     k_append,
    ...     v_append,
    ...     kv_append_indptr,
    ...     paged_kv_cache,
    ...     kv_page_indices,
    ...     kv_page_indptr,
    ...     kv_last_page_len
    ... )

    Note
    ----
    Please refer to the :ref:`tutorial <recursive-attention>` for a detailed
    explanation of the log-sum-exp function and attention states.

    The function assumes that the space for appended k/v have already been allocated,
    which means :attr:`kv_indices`, :attr:`kv_indptr`, :attr:`kv_last_page_len` has
    incorporated appended k/v.
    """
    _check_kv_layout(kv_layout)
    _append_paged_kv_cache_kernel(
        append_key,
        append_value,
        append_indptr,
        *_unpack_paged_kv_cache(paged_kv_cache, kv_layout),
        kv_indices,
        kv_indptr,
        kv_last_page_len,
        TensorLayout[kv_layout].value,
    )
