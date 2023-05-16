# Copyright 2023 Serket authors
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

import functools as ft

import jax
import kernex as kex
import pytreeclass as pytc

from serket.nn.utils import validate_spatial_ndim


class Laplace2D(pytc.TreeClass):
    def __init__(self):
        # apply laplace operator on channel axis
        @jax.vmap
        @kex.kmap(kernel_size=(3, 3), strides=(1, 1), padding="SAME")
        def op(x):
            return -4 * x[1, 1] + x[0, 1] + x[2, 1] + x[1, 0] + x[1, 2]

        self._func = op

    @ft.partial(validate_spatial_ndim, attribute_name="spatial_ndim")
    def __call__(self, x: jax.Array, **k) -> jax.Array:
        return self._func(x)

    @property
    def spatial_ndim(self) -> int:
        return 2
