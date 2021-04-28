# Copyright 2021 Google LLC
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

import numpy as np
from collections import OrderedDict, Counter
from typing import Callable, Sequence, Tuple, Union
from warnings import warn
import itertools as it

from .maps import mesh
from . import maps
from . import PartitionSpec
from .. import core
from .. import linear_util as lu
from .._src.api import _check_callable, _check_arg
from .._src import source_info_util
from ..api_util import (argnums_partial_except, flatten_axes,
                        flatten_fun_nokwargs, _ensure_index_tuple,
                        donation_vector, rebase_donate_argnums)
from ..errors import JAXTypeError
from ..interpreters import ad
from ..interpreters import pxla
from ..interpreters import xla
from ..interpreters import partial_eval as pe
from ..lib import xla_bridge as xb
from ..lib import xla_client as xc
from ..tree_util import tree_flatten, tree_unflatten
from .._src.util import (extend_name_stack, HashableFunction, safe_zip,
                         wrap_name, wraps, distributed_debug_log,
                         as_hashable_function)
xops = xc._xla.ops

def pjit(fun: Callable,
         in_axis_resources,
         out_axis_resources,
         static_argnums: Union[int, Sequence[int]] = (),
         donate_argnums: Union[int, Sequence[int]] = ()):
  """Makes ``fun`` compiled and automatically partitioned using XLA.

  The returned function has semantics equivalent to those of ``fun``, but
  is compiled to a single multi-device XLA computation. The partitioning
  over multiple devices happens automatically, based on propagation of
  input partitioning specified in ``in_axis_resources`` and output partitioning
  specified in ``out_axis_resources``. The resources specified in those two
  arguments can only refer to mesh axes, as defined by the
  :py:func:`jax.experimental.maps.mesh` context manager. Note that the mesh
  definition at ``pjit`` application time is ignored, and the returned function
  will use the mesh definition available at each call site.

  Inputs to ``pjit`` do not have to be partitioned accross multiple devices.
  The implementation ensures to do the sharding automatically in that case.
  However, for best performance it is best to ensure that the arguments
  are correctly sharded (e.g. they are output of another ``pjit`` with
  ``out_axis_resources`` matching the desired ``in_axis_resources``).

  .. note::
    Standard caveats apply to multi-process JAX invocations. ``pjit`` can operate
    over meshes spanning multiple processes, but in that case the input dimensions
    partitioned over multi-process mesh axes are expected to be of size matching
    the size of the local mesh axis. The invocation of the ``pjit``ed function
    will be then equivalent to running a computation with a global view of
    the input array. In particular all elements coming from all processes are
    visible to the computation. Outputs in each process will also correspond to
    the slices of the global value that correspond to the data partitioned onto
    the local devices.

  Args:
    fun: Function to be compiled. Should be a pure function, as side-effects may
      only be executed once. Its arguments and return value should be arrays,
      scalars, or (nested) standard Python containers (tuple/list/dict) thereof.
      Positional arguments indicated by ``static_argnums`` can be anything at
      all, provided they are hashable and have an equality operation defined.
      Static arguments are included as part of a compilation cache key, which is
      why hash and equality operators must be defined.
    in_axis_resources: Pytree of structure matching that of arguments to ``fun``,
      with all actual arguments replaced by resource assignment specifications.
      It is also valid to specify a pytree prefix (e.g. one value in place of a
      whole subtree), in which case the leaves get broadcast to all values in
      that subtree.

      The valid resource assignment specifications are:
        - :py:obj:`None`, in which case the value will be replicated on all devices
        - :py:class:`PartitionSpec`, a tuple of length at most equal to the rank
          of the partitioned value. Each element can be a :py:obj:`None`, a mesh
          axis or a tuple of mesh axes, and specifies the set of resources assigned
          to partition the value's dimension matching its position in the spec.

      The size of every dimension has to be a multiple of the total number of
      resources assigned to it.
    out_axis_resources: Like ``in_axis_resources``, but specifies resource
      assignment for function outputs.
    static_argnums: An optional int or collection of ints that specify which
      positional arguments to treat as static (compile-time constant).
      Operations that only depend on static arguments will be constant-folded in
      Python (during tracing), and so the corresponding argument values can be
      any Python object.

      Static arguments should be hashable, meaning both ``__hash__`` and
      ``__eq__`` are implemented, and immutable. Calling the jitted function
      with different values for these constants will trigger recompilation.
      Arguments that are not arrays or containers thereof must be marked as
      static.

      If ``static_argnums`` is not provided, no arguments are treated as static.
    donate_argnums: Specify which arguments are "donated" to the computation.
      It is safe to donate arguments if you no longer need them once the
      computation has finished. In some cases XLA can make use of donated
      buffers to reduce the amount of memory needed to perform a computation,
      for example recycling one of your input buffers to store a result. You
      should not reuse buffers that you donate to a computation, JAX will raise
      an error if you try to.

  Returns:
    A wrapped version of ``fun``, set up for just-in-time compilation and
    automatic partitioned by the mesh available at each call site.

  For example, a convolution operator can be automatically partitioned over
  an arbitrary set of devices by a single ```pjit`` application:

  >>> import jax
  >>> import jax.numpy as jnp
  >>> from jax.experimental.pjit import PartitionSpec, pjit, mesh
  >>>
  >>> x = jnp.arange(8, dtype=jnp.float32)
  >>> f = pjit(lambda x: jax.numpy.convolve(x, jnp.asarray([0.5, 1.0, 0.5]), 'same'),
  ...         in_axis_resources=None, out_axis_resources=PartitionSpec('devices'))
  >>> with mesh(jax.devices(), ('devices',)):
  ...   print(f(x))  # doctest: +SKIP
  [ 0.5  2.   4.   6.   8.  10.  12.  10. ]
  """
  warn("pjit is an experimental feature and probably has bugs!")
  _check_callable(fun)

  # To be a tree prefix of the positional args tuple, in_axes can never be a
  # list: if in_axes is not a leaf, it must be a tuple of trees. However,
  # in cases like these users expect tuples and lists to be treated
  # essentially interchangeably, so we canonicalize lists to tuples here
  # rather than raising an error. https://github.com/google/jax/issues/2367
  if isinstance(in_axis_resources, list):
    in_axis_resources = tuple(in_axis_resources)
  if isinstance(out_axis_resources, list):
    out_axis_resources = tuple(out_axis_resources)

  in_axis_resources, in_axis_resources_entries, _ = \
      _prepare_axis_resources(in_axis_resources, "in_axis_resources")
  out_axis_resources, out_axis_resources_entries, out_axis_treedef = \
      _prepare_axis_resources(out_axis_resources, "out_axis_resources")
  out_axis_resources_entries = tuple(out_axis_resources_entries)

  static_argnums = _ensure_index_tuple(static_argnums)
  donate_argnums = _ensure_index_tuple(donate_argnums)
  donate_argnums = rebase_donate_argnums(donate_argnums, static_argnums)

  @wraps(fun)
  def wrapped(*args, **kwargs):
    if kwargs:
      raise NotImplementedError("pjit over kwargs not yet supported")
    if max(static_argnums + donate_argnums, default=-1) >= len(args):
      raise ValueError(f"jitted function has static_argnums={static_argnums}, "
                       f"donate_argnums={donate_argnums} but "
                       f"was called with only {len(args)} positional arguments.")

    # Putting this outside of wrapped would make resources lexically scoped
    resource_env = maps.thread_resources.env

    f = lu.wrap_init(fun)
    if static_argnums:
      f, dyn_args = argnums_partial_except(
          f, static_argnums, args, allow_invalid=False)
    else:
      dyn_args = args

    args_flat, in_tree = tree_flatten(args)
    for arg in args_flat: _check_arg(arg)
    flat_fun, out_tree = flatten_fun_nokwargs(f, in_tree)
    if donate_argnums:
      donated_invars = donation_vector(donate_argnums, dyn_args, ())
    else:
      donated_invars = (False,) * len(args_flat)

    in_axis_resources_flat = tuple(flatten_axes("pjit in_axis_resources",
                                                in_tree, in_axis_resources))
    out_axis_resources_thunk = HashableFunction(
        lambda: tuple(flatten_axes("pjit out_axis_resources", out_tree(),
                                   out_axis_resources)),
        closure=(out_axis_resources_entries, out_axis_treedef))
    _check_shapes_against_resources("pjit arguments", resource_env,
                                    args_flat, in_axis_resources_flat)
    flat_fun = _check_output_shapes(flat_fun, resource_env,
                                    out_axis_resources_thunk)

    out = pjit_call_p.bind(
        flat_fun,
        *args_flat,
        in_axis_resources=in_axis_resources_flat,
        out_axis_resources_thunk=out_axis_resources_thunk,
        resource_env=resource_env,
        donated_invars=donated_invars,
        name=flat_fun.__name__)
    return tree_unflatten(out_tree(), out)

  return wrapped

class ParsedPartitionSpec:
  def __init__(self, user_spec, partitions):
    self.partitions = tuple(partitions)
    self.user_spec = user_spec

  @classmethod
  def from_user_input(cls, entry, arg_name):
    if entry is None:
      return cls(entry, ())
    if not isinstance(entry, PartitionSpec):
      raise TypeError(f"{arg_name} are expected to be "
                      f"PartitionSpec instances or None, but got {entry}")
    axis_specs = []
    for axis_spec in entry:
      if axis_spec is None:
        axis_spec = ()
      elif isinstance(axis_spec, (list, tuple)):
        axis_spec = tuple(axis_spec)
      else:
        axis_spec = (axis_spec,)
      axis_specs.append(axis_spec)
    return cls(entry, axis_specs)

  def __hash__(self):
    return hash(self.partitions)

  def __eq__(self, other):
    return (self.partitions, self.user_spec) == (other.partitions, other.user_spec)

  def __str__(self):
    return str(self.user_spec)

  def __len__(self):
    return len(self.partitions)

  def __getitem__(self, i):
    return self.partitions[i]

  def __iter__(self):
    return iter(self.partitions)

REPLICATED = ParsedPartitionSpec(None, ())


def _prepare_axis_resources(axis_resources, arg_name):
  if axis_resources is None:
    return REPLICATED, [REPLICATED], tree_flatten(None)[1]
  # PyTrees don't treat None values as leaves, so we explicitly need
  # to explicitly declare them as such
  entries, treedef = tree_flatten(axis_resources, is_leaf=lambda x: x is None)
  what = f"{arg_name} leaf specifications"
  entries = [ParsedPartitionSpec.from_user_input(entry, what) for entry in entries]
  _check_unique_resources(entries, arg_name)
  return tree_unflatten(treedef, entries), entries, treedef

def _check_unique_resources(axis_resources, arg_name):
  for arg_axis_resources in axis_resources:
    if not arg_axis_resources: continue
    resource_counts = Counter(it.chain.from_iterable(arg_axis_resources))
    if resource_counts.most_common(1)[0][1] > 1:
      multiple_uses = [r for r, c in resource_counts.items() if c > 1]
      if multiple_uses:
        raise ValueError(f"A single {arg_name} specification can map every mesh axis "
                         f"to at most one positional dimension, but {arg_axis_resources} "
                         f"has duplicate entries for {maps.show_axes(multiple_uses)}")

@lu.transformation
def _check_output_shapes(resource_env, out_axis_resources_thunk, *args, **kwargs):
  outputs = yield (args, kwargs)
  _check_shapes_against_resources("pjit outputs", resource_env, outputs, out_axis_resources_thunk())
  yield outputs

def _check_shapes_against_resources(what: str, resource_env, flat_vals, flat_axis_resources):
  resource_sizes = resource_env.local_shape
  for val, aval_axis_resources in zip(flat_vals, flat_axis_resources):
    shape = core.get_aval(val).shape
    if len(shape) < len(aval_axis_resources):
      raise ValueError(f"One of {what} was given the resource assignment "
                       f"of {aval_axis_resources}, which implies that "
                       f"it has a rank of at least {len(aval_axis_resources)}, "
                       f"but it is {len(shape)}")
    for i, axis_resources in enumerate(aval_axis_resources):
      try:
        size = int(np.prod([resource_sizes[resource] for resource in axis_resources], dtype=np.int64))
      except KeyError as e:
        raise ValueError(f"One of {what} was given the resource assignment "
                         f"of {aval_axis_resources}, but resource axis "
                         f"{e.args[0]} is undefined. Did you forget to declare the mesh?")
      if shape[i] % size != 0:
        raise ValueError(f"One of {what} was given the resource assignment "
                         f"of {aval_axis_resources}, which implies that "
                         f"the size of its dimension {i} should be divisible by "
                         f"{size}, but it is equal to {shape[i]}")

def _pjit_call_impl(fun: lu.WrappedFun, *args, in_axis_resources,
                    out_axis_resources_thunk, resource_env, donated_invars,
                    name):
  in_avals = [core.raise_to_shaped(core.get_aval(arg)) for arg in args]
  pjit_callable = _pjit_callable(
      fun, in_axis_resources, out_axis_resources_thunk, resource_env,
      donated_invars, name, *in_avals)
  distributed_debug_log(("Running pjit'd function", name),
                        ("python function", fun.f),
                        ("mesh", resource_env.physical_mesh),
                        ("abstract args", in_avals))
  return pjit_callable(*args)

pjit_call_p = core.CallPrimitive("pjit_call")
pjit_call_p.def_impl(_pjit_call_impl)

@lu.cache
def _pjit_callable(
    fun: lu.WrappedFun,
    in_axis_resources: Tuple[ParsedPartitionSpec, ...],
    out_axis_resources_thunk: Callable[[], Tuple[ParsedPartitionSpec, ...]],
    resource_env,
    donated_invars,
    name: str,
    *local_in_avals):

  in_axes = [get_array_mapping(axes) for axes in in_axis_resources]
  out_axes = lambda: [get_array_mapping(axes) for axes in out_axis_resources_thunk()]
  # TODO(skye): allow for using a submesh of physical_mesh
  return pxla.mesh_callable(fun, name, None, resource_env.physical_mesh,
                            in_axes, out_axes, donated_invars,
                            True, *local_in_avals, tile_by_mesh_axes=False,
                            do_resource_typecheck="pjit")

# -------------------- pjit rules --------------------

def _pjit_translation_rule(c, axis_env, in_nodes, name_stack, backend, name,
                           call_jaxpr, in_axis_resources, out_axis_resources,
                           resource_env, donated_invars):
  mesh = resource_env.physical_mesh
  subc = xc.XlaBuilder(f"pjit_{name}")

  args = []
  for i, (n, axis_resources) in enumerate(safe_zip(in_nodes, in_axis_resources)):
    # N.B. inlined calls shouldn't have shardings set directly on the inputs or
    # outputs (set_sharding_proto adds an identity operation).
    arg = xb.parameter(subc, i, c.GetShape(n))
    args.append(xb.set_sharding_proto(subc, arg,
                                      get_sharding_proto(c, n, axis_resources, mesh)))

  out_nodes = xla.jaxpr_subcomp(
      subc, call_jaxpr, backend, axis_env, (),
      extend_name_stack(name_stack, wrap_name(name, "pjit")), *args)
  out_nodes = [
      xb.set_sharding_proto(subc, out,
                            get_sharding_proto(subc, out, axis_resources, mesh))
      for out, axis_resources in safe_zip(out_nodes, out_axis_resources)
  ]

  subc = subc.build(xops.Tuple(subc, out_nodes))
  return xops.Call(c, subc, list(in_nodes))
xla.call_translations[pjit_call_p] = _pjit_translation_rule

def _pjit_partial_eval_update_params(params, in_unknowns):
  call_jaxpr = params['call_jaxpr']
  donated_invars = params['donated_invars']
  in_axis_resources = params['in_axis_resources']
  out_axis_resources_thunk = params['out_axis_resources_thunk']
  if not in_unknowns and donated_invars:
    # JaxprTrace.post_process_call creates a call with no input tracers
    new_donated_invars = (False,) * len(call_jaxpr.invars)
    new_in_axis_resources = (REPLICATED,) * len(call_jaxpr.invars)
  else:
    # JaxprTrace.process_call drops known input tracers and prepends constants
    num_consts = len(call_jaxpr.invars) - len(donated_invars)
    def filter_unknown(l):
      return tuple(x for x, uk in zip(l, in_unknowns) if uk)
    new_donated_invars = (False,) * num_consts + filter_unknown(donated_invars)
    new_in_axis_resources = ((REPLICATED,) * num_consts +
                             filter_unknown(in_axis_resources))
  new_out_axis_resources = out_axis_resources_thunk()
  new_params = dict(params,
                    donated_invars=new_donated_invars,
                    in_axis_resources=new_in_axis_resources,
                    out_axis_resources=new_out_axis_resources)
  del new_params['out_axis_resources_thunk']
  return new_params
pe.call_param_updaters[pjit_call_p] = _pjit_partial_eval_update_params

def _pjit_jvp_update_params(params, nz_tangents, nz_tangents_out_thunk):
  donated_invars = params['donated_invars']
  in_axis_resources = params['in_axis_resources']
  out_axis_resources_thunk = params['out_axis_resources_thunk']
  def filter_nonzero_ins(l):
    return tuple(x for x, nz in zip(l, nz_tangents) if nz)
  @as_hashable_function(closure=(tuple(nz_tangents), out_axis_resources_thunk))
  def new_out_axis_resources_thunk():
    out_axis_resources = out_axis_resources_thunk()
    return (*out_axis_resources,
            *(ax for ax, nz in zip(out_axis_resources, nz_tangents_out_thunk()) if nz))
  return dict(params,
              donated_invars=(donated_invars + filter_nonzero_ins(donated_invars)),
              in_axis_resources=(in_axis_resources + filter_nonzero_ins(in_axis_resources)),
              out_axis_resources_thunk=new_out_axis_resources_thunk)
ad.call_param_updaters[pjit_call_p] = _pjit_jvp_update_params


def _pjit_init_to_final_params(params):
  out_axis_resources_thunk = HashableFunction(lambda: params['out_axis_resources'],
                                              closure=params['out_axis_resources'])
  bind_params = dict(params, out_axis_resources_thunk=out_axis_resources_thunk)
  del bind_params['out_axis_resources']
  return bind_params
core.initial_to_final_param_rules[pjit_call_p] = _pjit_init_to_final_params

def _check_resources_against_named_axes(what, aval, pos_axis_resources, named_axis_resources):
  pjit_resources = set(it.chain.from_iterable(pos_axis_resources))
  aval_resources = set(it.chain.from_iterable(
    named_axis_resources[a] for a in aval.named_shape))
  overlap = pjit_resources & aval_resources
  if overlap:
    raise JAXTypeError(
        f"{what} has an axis resources specification of {pos_axis_resources} "
        f"that uses one or more mesh axes already used by xmap to partition "
        f"a named axis appearing in its named_shape (both use mesh axes "
        f"{maps.show_axes(overlap)})")

def _resource_typing_pjit(avals, params, source_info, named_axis_resources):
  jaxpr = params['call_jaxpr']
  what = "pjit input"
  for v, pos_axis_resources in zip(jaxpr.invars, params['in_axis_resources']):
    _check_resources_against_named_axes(what, v.aval, pos_axis_resources, named_axis_resources)
  pxla.resource_typecheck(
      jaxpr, named_axis_resources,
      lambda: (f"a pjit'ed function {params['name']} "
               f"(pjit called at {source_info_util.summarize(source_info)})"))
  what = "pjit output"
  for v, pos_axis_resources in zip(jaxpr.outvars, params['out_axis_resources']):
    _check_resources_against_named_axes(what, v.aval, pos_axis_resources, named_axis_resources)
pxla.custom_resource_typing_rules[pjit_call_p] = _resource_typing_pjit


# -------------------- with_sharding_constraint --------------------

def with_sharding_constraint(x, axis_resources):
  x_flat, tree = tree_flatten(x)
  parsed_axis_resources, entries, _ = _prepare_axis_resources(axis_resources, "axis_resources")
  axis_resources_flat = tuple(
      flatten_axes("with_sharding_constraint axis_resources",
                   tree, parsed_axis_resources))
  resource_env = maps.thread_resources.env
  _check_shapes_against_resources(
      "with_sharding_constraint arguments",
      resource_env, x_flat, axis_resources_flat)
  outs = [sharding_constraint_p.bind(y, axis_resources=r, resource_env=resource_env)
          for y, r in safe_zip(x_flat, axis_resources_flat)]
  return tree_unflatten(tree, outs)

def _sharding_constraint_impl(x, axis_resources, resource_env):
  # TODO(skye): can we also prevent this from being called in other
  # non-pjit contexts? (e.g. pmap, control flow)
  raise NotImplementedError(
      "with_sharding_constraint() should only be called inside pjit()")

def _sharding_constraint_translation_rule(c, x_node, axis_resources, resource_env):
  mesh = resource_env.physical_mesh
  return xb.set_sharding_proto(c, x_node,
                               get_sharding_proto(c, x_node, axis_resources, mesh))

sharding_constraint_p = core.Primitive("sharding_constraint")
sharding_constraint_p.def_impl(_sharding_constraint_impl)
sharding_constraint_p.def_abstract_eval(lambda x, **unused: x)
ad.deflinear2(sharding_constraint_p,
              lambda ct, _, axis_resources, resource_env: (
                  sharding_constraint_p.bind(
                      ct, axis_resources=axis_resources, resource_env=resource_env),))
xla.translations[sharding_constraint_p] = _sharding_constraint_translation_rule

def _resource_typing_sharding_constraint(avals, params, source_info, named_axis_resources):
  aval, = avals
  _check_resources_against_named_axes(
    "with_sharding_constraint input", aval,
    params['axis_resources'], named_axis_resources)
pxla.custom_resource_typing_rules[sharding_constraint_p] = \
    _resource_typing_sharding_constraint

# -------------------- helpers --------------------

def get_array_mapping(axis_resources: ParsedPartitionSpec) -> pxla.ArrayMapping:
  return OrderedDict((axis, i)
                     for i, axes in enumerate(axis_resources)
                     for axis in axes)

def get_sharding_proto(c, xla_op, axis_resources, mesh):
  xla_shape = c.GetShape(xla_op)
  if xla_shape.is_token():
    aval = core.abstract_token
    assert axis_resources is REPLICATED
  else:
    aval = core.ShapedArray(xla_shape.dimensions(), xla_shape.element_type())
  array_mapping = get_array_mapping(axis_resources)
  sharding_spec = pxla.mesh_sharding_specs(mesh.shape, mesh.axis_names)(
      aval, array_mapping)
  return sharding_spec.sharding_proto()
