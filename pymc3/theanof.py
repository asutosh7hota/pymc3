import numpy as np

from .vartypes import typefilter, continuous_types
from theano import theano, scalar, tensor as tt
from theano.gof.graph import inputs
from theano.gof import Op
from theano.configparser import change_flags
from .memoize import memoize
from .blocking import ArrayOrdering
from .data import DataGenerator

__all__ = ['gradient', 'hessian', 'hessian_diag', 'inputvars',
           'cont_inputs', 'floatX', 'jacobian',
           'CallableTensor', 'join_nonshared_inputs',
           'make_shared_replacements', 'generator']


def inputvars(a):
    """
    Get the inputs into a theano variables

    Parameters
    ----------
        a : theano variable

    Returns
    -------
        r : list of tensor variables that are inputs
    """
    return [v for v in inputs(makeiter(a)) if isinstance(v, tt.TensorVariable)]


def cont_inputs(f):
    """
    Get the continuous inputs into a theano variables

    Parameters
    ----------
        a : theano variable

    Returns
    -------
        r : list of tensor variables that are continuous inputs
    """
    return typefilter(inputvars(f), continuous_types)


def floatX(X):
    """
    Convert a theano tensor or numpy array to theano.config.floatX type.
    """
    try:
        return X.astype(theano.config.floatX)
    except AttributeError:
        # Scalar passed
        return np.asarray(X, dtype=theano.config.floatX)

"""
Theano derivative functions
"""

def gradient1(f, v):
    """flat gradient of f wrt v"""
    return tt.flatten(tt.grad(f, v, disconnected_inputs='warn'))


empty_gradient = tt.zeros(0, dtype='float32')


@memoize
def gradient(f, vars=None):
    if vars is None:
        vars = cont_inputs(f)

    if vars:
        return tt.concatenate([gradient1(f, v) for v in vars], axis=0)
    else:
        return empty_gradient


def jacobian1(f, v):
    """jacobian of f wrt v"""
    f = tt.flatten(f)
    idx = tt.arange(f.shape[0], dtype='int32')

    def grad_i(i):
        return gradient1(f[i], v)

    return theano.map(grad_i, idx)[0]


@memoize
def jacobian(f, vars=None):
    if vars is None:
        vars = cont_inputs(f)

    if vars:
        return tt.concatenate([jacobian1(f, v) for v in vars], axis=1)
    else:
        return empty_gradient


@memoize
def hessian(f, vars=None):
    return -jacobian(gradient(f, vars), vars)


def hessian_diag1(f, v):
    g = gradient1(f, v)
    idx = tt.arange(g.shape[0], dtype='int32')

    def hess_ii(i):
        return gradient1(g[i], v)[i]

    return theano.map(hess_ii, idx)[0]


@memoize
def hessian_diag(f, vars=None):
    if vars is None:
        vars = cont_inputs(f)

    if vars:
        return -tt.concatenate([hessian_diag1(f, v) for v in vars], axis=0)
    else:
        return empty_gradient


def makeiter(a):
    if isinstance(a, (tuple, list)):
        return a
    else:
        return [a]


class IdentityOp(scalar.UnaryScalarOp):

    @staticmethod
    def st_impl(x):
        return x

    def impl(self, x):
        return x

    def grad(self, inp, grads):
        return grads

    def c_code(self, node, name, inp, out, sub):
        return "{z} = {x};".format(x=inp[0], z=out[0])

    def __eq__(self, other):
        return type(self) == type(other)

    def __hash__(self):
        return hash(type(self))


def make_shared_replacements(vars, model):
    """
    Makes shared replacements for all *other* variables than the ones passed.

    This way functions can be called many times without setting unchanging variables. Allows us
    to use func.trust_input by removing the need for DictToArrayBijection and kwargs.

    Parameters
    ----------
    vars : list of variables not to make shared
    model : model

    Returns
    -------
    Dict of variable -> new shared variable
    """
    othervars = set(model.vars) - set(vars)
    return {var: theano.shared(var.tag.test_value, var.name + '_shared') for var in othervars}


def join_nonshared_inputs(xs, vars, shared, make_shared=False):
    """
    Takes a list of theano Variables and joins their non shared inputs into a single input.

    Parameters
    ----------
    xs : list of theano tensors
    vars : list of variables to join

    Returns
    -------
    tensors, inarray
    tensors : list of same tensors but with inarray as input
    inarray : vector of inputs
    """
    joined = tt.concatenate([var.ravel() for var in vars])

    if not make_shared:
        tensor_type = joined.type
        inarray = tensor_type('inarray')
    else:
        inarray = theano.shared(joined.tag.test_value, 'inarray')

    ordering = ArrayOrdering(vars)
    inarray.tag.test_value = joined.tag.test_value

    get_var = {var.name: var for var in vars}
    replace = {
        get_var[var]: reshape_t(inarray[slc], shp).astype(dtyp)
        for var, slc, shp, dtyp in ordering.vmap}

    replace.update(shared)

    xs_special = [theano.clone(x, replace, strict=False) for x in xs]
    return xs_special, inarray


def reshape_t(x, shape):
    """Work around fact that x.reshape(()) doesn't work"""
    if shape != ():
        return x.reshape(shape)
    else:
        return x[0]


class CallableTensor(object):
    """Turns a symbolic variable with one input into a function that returns symbolic arguments
    with the one variable replaced with the input.
    """

    def __init__(self, tensor):
        self.tensor = tensor

    def __call__(self, input):
        """ Replaces the single input of symbolic variable to be the passed argument.

        Parameters
        ----------
        input : TensorVariable
        """
        oldinput, = inputvars(self.tensor)
        return theano.clone(self.tensor, {oldinput: input}, strict=False)

scalar_identity = IdentityOp(scalar.upgrade_to_float, name='scalar_identity')
identity = tt.Elemwise(scalar_identity, name='identity')


class GeneratorOp(Op):
    """
    Generaror Op is designed for storing python generators
    inside theano graph. The main limitation is generator itself.

    There are some important cases when generator becomes exhausted
        - not endless generator is passed
        - exception is raised while `generator.__next__` is performed
            Note: it is dangerous in simple python generators, but ok in
            custom class based generators with explicit state
        - data type on each iteration should be the same
    """
    __props__ = ('generator',)

    def __init__(self, gen):
        super(GeneratorOp, self).__init__()
        if not isinstance(gen, DataGenerator):
            gen = DataGenerator(gen)
        self.generator = gen
        self.itypes = []
        self.otypes = [self.generator.tensortype]
        self._nan = np.zeros_like(self.generator.test_value)
        self._nan[...] = np.nan

    def perform(self, node, inputs, output_storage, params=None):
        try:
            output_storage[0][0] = next(self.generator)
        except StopIteration:
            output_storage[0][0] = self._nan

    def do_constant_folding(self, node):
        return False

    class _set_gen(object):
        """For pickling"""
        def __init__(self, op):
            self.op = op

        def __call__(self, gen):
            self.op.set_gen(gen)

    @change_flags(compute_test_value='off')
    def __call__(self, *args, **kwargs):
        rval = super(GeneratorOp, self).__call__(*args, **kwargs)
        rval.set_gen = self._set_gen(self)
        rval.tag.test_value = self.generator.test_value
        return rval

    def set_gen(self, gen):
        if not isinstance(gen, DataGenerator):
            gen = DataGenerator(gen)
        if not gen.tensortype == self.generator.tensortype:
            raise ValueError('New generator should yield the same type')
        self.generator = gen


def generator(gen):
    """shortcut for `GeneratorOp`"""
    return GeneratorOp(gen)()
