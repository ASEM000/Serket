# Copyright 2024 serket authors
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
from typing import Callable, get_args

import jax
import jax.nn.initializers as ji
import jax.numpy as jnp
import jax.tree_util as jtu

from serket._src.utils.typing import InitFuncType, InitLiteral, InitType

inits: list[InitType] = [
    ji.he_normal(in_axis=1, out_axis=0),
    ji.he_uniform(in_axis=1, out_axis=0),
    ji.glorot_normal(in_axis=1, out_axis=0),
    ji.glorot_uniform(in_axis=1, out_axis=0),
    ji.lecun_normal(in_axis=1, out_axis=0),
    ji.lecun_uniform(in_axis=1, out_axis=0),
    ji.normal(),
    ji.uniform(),
    ji.ones,
    ji.zeros,
    ji.xavier_normal(in_axis=1, out_axis=0),
    ji.xavier_uniform(in_axis=1, out_axis=0),
    ji.orthogonal(),
]


init_map: dict[str, Callable[..., InitType]] = dict(zip(get_args(InitLiteral), inits))


def resolve_init(init) -> jtu.Partial[InitFuncType]:
    if isinstance(init, str):
        try:
            return jtu.Partial(jax.tree_map(lambda x: x, init_map[init]))
        except KeyError:
            raise ValueError(f"Unknown {init=}, available init: {list(init_map)}")
    if init is None:
        return jtu.Partial(lambda key, shape, dtype=None: None)
    if isinstance(init, ABCCallable):
        return jtu.Partial(init)
    raise TypeError(f"Unknown type {type(init)}")
