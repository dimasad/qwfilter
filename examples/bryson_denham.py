"""Bryson--Denham optimal control problem."""


import numpy as np
import sympy
import sym2num.model
import sym2num.var

from ceacoest import symb_oc


@symb_oc.collocate(order=3)
class BrysonDenham:
    """Symbolic Bryson--Denham optimal control model."""
        
    @sym2num.model.make_variables_dict
    def variables():
        """Model variables definition."""
        return [
            sym2num.var.SymbolArray('x', ['x1', 'x2', 'x3']),
            sym2num.var.SymbolArray('u', ['u1']),
            sym2num.var.SymbolArray('p', []),
        ]
    
    @sym2num.model.symbols_from('x, u, p')
    def f(self, s):
        """ODE function."""
        return sympy.Array([s.x2, s.u1, 0.5*s.u1**2])
    
    @sym2num.model.symbols_from('x, u, p')
    def g(self, s):
        """Path constraints."""
        return sympy.Array([], 0)
    
    @sym2num.model.symbols_from('xe, p, T')
    def h(self, s):
        """Endpoint constraints."""
        return sympy.Array([], 0)
    
    @sym2num.model.symbols_from('xe, p, T')
    def M(self, s):
        """Mayer (endpoint) cost."""
        return sympy.Array(s.x3_end)


if __name__ == '__main__':
    symb_mdl = BrysonDenham()
    GeneratedBrysonDenham = sym2num.model.compile_class(
        'GeneratedBrysonDenham', symb_mdl
    )
    mdl = GeneratedBrysonDenham()

    from ceacoest import oc_
    t = np.linspace(0, 1, 20)
    problem = oc_.Problem(mdl, t)
