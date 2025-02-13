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

import jax

import serket as sk


def test_sequential():
    model = sk.Sequential(lambda x: x)
    assert model(1.0, key=jax.random.key(0)) == 1.0

    model = sk.Sequential(lambda x: x + 1, lambda x: x + 1)
    assert model(1.0, key=jax.random.key(0)) == 3.0

    model = sk.Sequential(lambda x, key: x)
    assert model(1.0, key=jax.random.key(0)) == 1.0


def test_sequential_getitem():
    model = sk.Sequential(lambda x: x + 1, lambda x: x + 1)
    assert model[0](1.0) == 2.0
    assert model[1](1.0) == 2.0
    assert model[0:1](1.0, key=jax.random.key(0)) == 2.0
    assert model[1:2](1.0, key=jax.random.key(0)) == 2.0
    assert model[0:2](1.0, key=jax.random.key(0)) == 3.0


def test_sequential_len():
    model = sk.Sequential(lambda x: x + 1, lambda x: x + 1)
    assert len(model) == 2


def test_sequential_iter():
    model = sk.Sequential(lambda x: x + 1, lambda x: x + 1)
    assert list(model) == [model[0], model[1]]


def test_sequential_reversed():
    model = sk.Sequential(lambda x: x + 1, lambda x: x + 1)
    assert list(reversed(model)) == [model[1], model[0]]
