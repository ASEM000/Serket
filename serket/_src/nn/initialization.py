# Copyright 2023 serket authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import annotations

from collections.abc import Callable as ABCCallable
from typing import Any, Callable, Literal, Tuple, Union, get_args

import jax
import jax.nn.initializers as ji
import jax.tree_util as jtu
import numpy as np

from serket._src.utils import single_dispatch

InitLiteral = Literal[
    "he_normal",
    "he_uniform",
    "glorot_normal",
    "glorot_uniform",
    "lecun_normal",
    "lecun_uniform",
    "normal",
    "uniform",
    "ones",
    "zeros",
    "xavier_normal",
    "xavier_uniform",
    "orthogonal",
]

Shape = Tuple[int, ...]
DType = Union[np.dtype, str, Any]
InitFuncType = Callable[[jax.Array, Shape, DType], jax.Array]
InitType = Union[InitLiteral, InitFuncType]


inits: list[InitFuncType] = [
    ji.he_normal(),
    ji.he_uniform(),
    ji.glorot_normal(),
    ji.glorot_uniform(),
    ji.lecun_normal(),
    ji.lecun_uniform(),
    ji.normal(),
    ji.uniform(),
    ji.ones,
    ji.zeros,
    ji.xavier_normal(),
    ji.xavier_uniform(),
    ji.orthogonal(),
]

init_map: dict[str, InitType] = dict(zip(get_args(InitLiteral), inits))


@single_dispatch(argnum=0)
def resolve_init(init):
    raise TypeError(f"Unknown type {type(init)}")


@resolve_init.def_type(str)
def _(init: str):
    try:
        return jtu.Partial(jax.tree_map(lambda x: x, init_map[init]))
    except KeyError:
        raise ValueError(f"Unknown {init=}, available init: {list(init_map)}")


@resolve_init.def_type(type(None))
def _(init: None):
    return jtu.Partial(lambda key, shape, dtype=None: None)


@resolve_init.def_type(ABCCallable)
def _(init: Callable):
    return jtu.Partial(init)
