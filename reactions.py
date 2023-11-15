import numpy as np
from enum import Enum
from typing import NamedTuple
from types import FunctionType as function

class Species():
    def __init__(self, name, abbreviation) -> None:
        self.name = name
        self.abbreviation = abbreviation

    def __hash__(self) -> int:
        return hash((self.name, self.abbreviation))

    def __repr__(self) -> str:
        return f"Species(name={self.name}, abbreviation={self.abbreviation})"

class MultiplicityType(Enum):
    reacants = 'reactants'
    products = 'products'
    stoichiometry = 'stoichiometry'
    rate_involvement = 'rate involvement'

class Reaction():
    def __init__(self, description, reactants, products, rate_involvement=None, k=None, reversible=False) -> None:
        assert reversible == False, "Reversible reactions are not supported. Create separate forward and back reactions instead."
        self.description = description

        self.k = k

        self.reactants = set([(r[0] if isinstance(r, tuple)  else r) for r in reactants])
        self.products = set([(p[0] if isinstance(p, tuple)  else p) for p in products])
        self.reactant_data = reactants
        self.product_data  = products

        self.rate_involvement = self.reactants if rate_involvement is None else rate_involvement

    def multiplicities(self, mult_type):
        multplicities = {}

        positive_multplicity_data = []
        negative_multiplicity_data = []
        if mult_type == MultiplicityType.reacants:
            positive_multplicity_data = self.reactant_data
        elif mult_type == MultiplicityType.products:
            positive_multplicity_data = self.product_data
        elif mult_type == MultiplicityType.stoichiometry:
            positive_multplicity_data = self.product_data
            negative_multiplicity_data = self.reactant_data
        elif mult_type == MultiplicityType.rate_involvement:
            positive_multplicity_data = self.rate_involvement
        else:
            raise ValueError(f"bad value for type of multiplicities to calculate: {mult_type}.")

        for species in negative_multiplicity_data:
            if isinstance(species, tuple):
                species, multiplicity = species
            else:
                multiplicity = 1
            multplicities[species] = -1 * multiplicity

        for species in positive_multplicity_data:
            if isinstance(species, tuple):
                species, multiplicity = species
            else:
                multiplicity = 1
            try:
                multplicities[species] += multiplicity
            except KeyError:
                multplicities[species] = multiplicity

        return multplicities

    def stoichiometry(self):
        return self.multiplicities(MultiplicityType.stoichiometry)

    def rate_involvement(self):
        return self.multiplicities(MultiplicityType.rate_involvement)

    def __repr__(self) -> str:
        return f"Reaction(description={self.description}, reactants={self.reactant_data}, products={self.product_data}, rate_involvement={self.rate_involvement}, k={self.k})"

class RateConstantCluster(NamedTuple):
    k: function
    slice_bottom: int
    slice_top: int

class Model():
    def __init__(self, species: list[Species], reactions: list[Reaction], jit=False) -> None:
        self.species = species
        self.reactions = []
        for r in reactions:
            if isinstance(r, Reaction):
                self.reactions.append(r)
            elif isinstance(r, ReactionRateFamily):
                self.reactions.extend(r.reactions)
            else:
                raise TypeError(f"bad type for reaction in model {type(r)}. Expected Reaction or ReactionRateFamily")


        self.n_species = len(self.species)
        self.n_reactions = len(self.reactions)

        # ReactionRateFamilies allow us to calculate k(t) for a group of reactions all at once
        base_k = np.zeros(self.n_reactions)
        k_of_ts = []
        i = 0
        # reactions not self.reactions so we see families
        for r in reactions:
            if isinstance(r, Reaction):
                if isinstance(r.k, float):
                    base_k[i] = r.k
                else:
                    assert(isinstance(r.k, function)), f"a reaction's rate constant should be a float or function with signature k(t) --> float: {r.k}"
                    k_of_ts.append(RateConstantCluster(r.k, i, i+1))
                i+=1
                continue
            # reaction rate family
            k_of_ts.append(RateConstantCluster(r.k, i, i+len(r.reactions)+1))
            i += len(r.reactions)

        self.base_k = base_k
        self.k_of_ts = k_of_ts

        self.species_index = {s:i for i,s in enumerate(self.species)}
        self.reaction_index = {r:i for i,r in enumerate(self.reactions)}

        if jit:
            # convert families into relevant lists
            self.k_jit = self.kjit_factory(np.array(self.base_k), self.k_of_ts)

    def kjit_factory(self, base_k, k_families):
        # k_jit can't be an ordinary method because we won't be able to have `self` as an argument in nopython
        # but needs to close around various properties of self, so we define as a closure using this factory function
        from numba import jit, float64
        from numba.types import Array
        # if we have no explicit time dependence, we our k function just returns base_k
        if len(k_families) == 0:
            @jit(Array(float64, 1, "C")(float64), nopython=True)
            def k_jit(t):
                k = base_k.copy()
                return k
            return k_jit
        # otherwise, we have to apply the familiy function to differently sized blocks
        k_functions, k_slice_bottoms, k_slice_tops = map(np.array, zip(*self.k_of_ts))

        if len(k_functions) > 1:
            raise JitNotImplementedError("building a nopython jit for the rate constants isn't supported with more than 1 subcomponent of k having explicit time dependence. Try using a ReactionRateFamily for all reactions and supplying a vectorized k.")

        # now we have noly one subcomponent we have to deal with
        k_function, k_slice_bottom, k_slice_top = k_functions[0], k_slice_bottoms[0], k_slice_tops[0]

        #@jit(Array(float64, 1, "C")(float64), nopython=True)
        @jit(nopython=True)
        def k_jit(t):
            k = base_k.copy()
            k[k_slice_bottom:k_slice_top] = k_function(t)
            return k

        return k_jit

    def multiplicity_matrix(self, mult_type):
        matrix = np.zeros((self.n_species, self.n_reactions))
        for column, reaction in enumerate(self.reactions):
            multiplicity_column = np.zeros(self.n_species)
            reaction_info = reaction.multiplicities(mult_type)
            for species, multiplicity in reaction_info.items():
                multiplicity_column[self.species_index[species]] = multiplicity

            matrix[:,column] = multiplicity_column

        return matrix

    def k(self, t):
        k = self.base_k.copy()
        for family in self.k_of_ts:
            k[family.slice_bottom:family.slice_top] = family.k(t)
        return k

    def stoichiometry(self):
        return self.multiplicity_matrix(MultiplicityType.stoichiometry)

    def rate_involvement(self):
        return self.multiplicity_matrix(MultiplicityType.rate_involvement)

    @staticmethod
    def pad_equally_until(string, length, tie='left'):
        missing_length = length - len(string)
        if tie == 'left':
            return " " * int(np.ceil(missing_length/2)) + string + " " * int(np.floor(missing_length/2))
        return " " * int(np.floor(missing_length/2)) + string + " " * int(np.ceil(missing_length/2))

    def pretty_side(self, reaction, side, absentee_value, skip_blanks=False):
        padded_length = 4
        reactant_multiplicities = reaction.multiplicities(side)
        if len(reactant_multiplicities.keys()) == 0:
            return self.pad_equally_until("0", padded_length)

        prior_species_flag = False
        pretty_side = ""
        for i,s in enumerate(self.species):
            mult = reactant_multiplicities.get(s, absentee_value)
            if mult is None:
                species_piece = '' if i==0 or skip_blanks else ' '*2
                pretty_side += species_piece
                if not skip_blanks:
                    pretty_side += " " * padded_length
                prior_species_flag = False
                continue
            if i == 0:
                species_piece = ''
            else:
                species_piece = ' +' if prior_species_flag else ' '*2
            prior_species_flag = True
            species_piece += self.pad_equally_until(f"{str(int(mult)) if mult < 10 else '>9':.2}{s.abbreviation:.2}", padded_length)
            #print(f"piece: |{species_piece}|")
            pretty_side += species_piece
        return pretty_side

    def pretty(self, hide_absent=True, skip_blanks=False) -> str:
        absentee_value = None if hide_absent else 0
        pretty = ""
        for reaction in self.reactions:
            pretty_reaction = f"{reaction.description:.22}" + " " * max(0, 22-len(reaction.description)) + ":"
            pretty_reaction += self.pretty_side(reaction, MultiplicityType.reacants, absentee_value, skip_blanks)
            pretty_reaction += ' --> '
            pretty_reaction += self.pretty_side(reaction, MultiplicityType.products, absentee_value, skip_blanks)

            pretty += pretty_reaction + '\n'
        return pretty

class JitNotImplementedError(Exception):
    pass

class ReactionRateFamily():
    def __init__(self, reactions, k) -> None:
        self.reactions = reactions
        self.k = k


if __name__ == '__main__':
    from numba import jit, float64
    from numba.types import Array

    @jit(Array(float64, 1, "C")(float64), nopython=True)
    def k_jit_family(t):
        return np.array([1.0, 2.0])

    a = Species("A", 'A')
    b = Species("B", 'B')
    r1 = Reaction("A+B->2A", [a,b], [(a,2)])
    r2 = Reaction("A->0", [a], [], k=2.)

    fam = ReactionRateFamily([r1,r2], k=k_jit_family)

    m = Model([a,b], [fam])
    m.k_jit(0.)