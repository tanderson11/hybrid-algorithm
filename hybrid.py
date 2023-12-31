import numpy as np
from scipy.integrate import solve_ivp
from typing import NamedTuple
from dataclasses import dataclass
from enum import IntEnum
from typing import Callable
from numba import jit
import json

@dataclass(frozen=True)
class SimulationOptions():
    jit: bool = True
    approximate_rtot: bool = False
    batch_events: bool = False
    deterministic: bool = True
    round_randomly: bool = True

@dataclass(frozen=True)
class SimulationResult():
    t: float
    y: np.ndarray
    n_stochastic: int
    nfev: int
    t_history: np.ndarray
    y_history: np.ndarray

def load_partition_scheme(file):
    with open(file, 'r') as f:
        dictionary = json.load(f)
    scheme_name = dictionary.pop('name')
    scheme_class = SCHEMES_BY_NAME[scheme_name]
    return scheme_class(**dictionary)

@dataclass
class PartitionScheme():
    def save(self, file):
        dictionary = {'name': type(self).__name__}
        dictionary.update(self.__dict__.copy())

        with open(file, 'w') as f:
            json.dump(dictionary, f)

@dataclass
class FixedThresholdPartitioner(PartitionScheme):
    threshold: float

    def partition_function(self, propensities):
        stochastic = (propensities <= self.threshold) & (propensities != 0)
        return Partition(~stochastic, stochastic)

SCHEMES_BY_NAME = {
    'FixedThresholdPartitioner': FixedThresholdPartitioner
}

@dataclass(frozen=True)
class Partition():
    deterministic: np.ndarray
    stochastic: np.ndarray

def stochastic_event_finder(t, y_expanded, k, deterministic_mask, stochastic_mask, hitting_point):
    stochastic_progress = y_expanded[-1]
    return stochastic_progress-hitting_point
stochastic_event_finder.terminal = True

def jit_calculate_propensities_factory(rate_involvement_matrix):
    @jit(nopython=True)
    def jit_calculate_propensities(y, k, t):
        # Remember, we want total number of distinct combinations * k === rate.
        # we want to calculate (y_i rate_involvement_ij) (binomial coefficient)
        # for each species i and each reaction j
        # sadly, inside a numba C function, we can't avail ourselves of scipy's binom,
        # so we write this little calculator ourselves
        intensity_power = np.zeros_like(rate_involvement_matrix)
        for i in range(0, rate_involvement_matrix.shape[0]):
            for j in range(0, rate_involvement_matrix.shape[1]):
                if y[i] < rate_involvement_matrix[i][j]:
                    intensity_power[i][j] = 0.0
                elif y[i] == rate_involvement_matrix[i][j]:
                    intensity_power[i][j] = 1.0
                else:
                    intensity = 1.0
                    for x in range(0, rate_involvement_matrix[i][j]):
                        intensity *= (y[i] - x) / (x+1)
                    intensity_power[i][j] = intensity

        # then we take the product down the columns (so product over each reaction)
        # and multiply that output by the vector of rate constants
        # to get the propensity of each reaction at time t
        k_of_t = k(t)
        product_down_columns = np.ones(len(k_of_t))
        for i in range(0, len(y)):
            product_down_columns = product_down_columns * intensity_power[i]
        return product_down_columns * k_of_t
    return jit_calculate_propensities

def jit_dydt_factory(N, jit_calculate_propensities):
    #@jit(float64(Array(float64, 1, "C"), Array(float64, 1, "C"), Array(float64, 1, "C")(float64), Array(float64, 2, "C"), Array(float64, 2, "C"), float64), nopython=True)
    @jit(nopython=True)
    def jit_dydt(t, y_expanded, k, deterministic_mask, stochastic_mask, hitting_point):
        # by fiat the last entry of y will carry the integral of stochastic rates
        y = y_expanded[:-1]

        propensities = jit_calculate_propensities(y, k, t)
        deterministic_propensities = propensities * deterministic_mask
        stochastic_propensities = propensities * stochastic_mask

        # each propensity feeds back into the stoich matrix to determine
        # overall rate of change in the state
        # https://en.wikipedia.org/wiki/Biochemical_systems_equation
        rates = N @ deterministic_propensities
        sum_stochastic = np.sum(stochastic_propensities)

        dydt = np.zeros_like(y_expanded)
        dydt[:-1] = rates
        dydt[-1]  = sum_stochastic
        #print("t", t, "y_expanded", y_expanded, "dydt", dydt)
        return dydt

    return jit_dydt

def dydt_factory(N, calculate_propensities):
    def dydt(t, y_expanded, k, deterministic_mask, stochastic_mask, hitting_point):
        # by fiat the last entry of y will carry the integral of stochastic rates
        y = y_expanded[:-1]
        #print("y at start of dydt", y)
        propensities = calculate_propensities(y, k, t)
        deterministic_propensities = propensities * deterministic_mask
        stochastic_propensities = propensities * stochastic_mask

        dydt = np.zeros_like(y_expanded)
        # each propensity feeds back into the stoich matrix to determine
        # overall rate of change in the state
        # https://en.wikipedia.org/wiki/Biochemical_systems_equation
        dydt[:-1] = N @ deterministic_propensities
        dydt[-1]  = np.sum(stochastic_propensities)
        #print("t", t, "y_expanded", y_expanded, "dydt", dydt)
        return dydt
    return dydt

def calculate_propensities_factory(rate_involvement_matrix):
    def calculate_propensities(y, k, t):
        # product along column in rate involvement matrix
        # with states raised to power of involvement
        # multiplied by rate constants == propensity
        # dimension of y is expanded to make it a column vector
        return np.prod(np.expand_dims(y, axis=1)**rate_involvement_matrix, axis=0) * k(t)
    return calculate_propensities

class StepStatus(IntEnum):
    upper_limit = 0
    stochastic_event = 1
    other_terminal_event = 2

class StepUpdate(NamedTuple):
    t: float
    y: np.ndarray
    status: StepStatus
    nfev: int
    t_history: np.ndarray
    y_history: np.ndarray

def canonicalize_event(t_events, y_events):
    event_index = None
    for i,event_set in enumerate(t_events):
        if event_set is None:
            continue
        if event_index is not None:
            assert False, "that's unusual, we had two different kinds of terminal events in 1 step"
        event_index = i
    # now we have one canonical event that occurred
    t_event = t_events[event_index]
    assert len(t_event) == 1, "we had more than one of the same kind of terminal event!"
    t_event = t_event[0]
    y_event = y_events[event_index][0]

    return t_event, y_event, event_index

def round_with_method(y, round_randomly=False, rng=None):
    if round_randomly:
        # round up if random float is less than decimal (small decimal ==> rarely round up)
        # round down otherwise
        rounded = (rng.random(y.shape) <= (y - np.floor(y))) + np.floor(y)
        return rounded
    else:
        return np.round(y)

def hybrid_step(
        calculate_propensities: Callable[[float, np.ndarray], np.ndarray],
        dydt: Callable,
        y0: np.ndarray,
        t_span: list,
        partition: Partition,
        k: Callable[[float], np.ndarray],
        N: np.ndarray,
        rate_involvement_matrix: np.ndarray,
        rng: np.random.Generator,
        events: list[Callable[[np.ndarray], float]],
        simulation_options: SimulationOptions,
        ) -> StepUpdate:
    """Integrates the partitioned system forward in time until reaching upper bound of integration or a stochastic event occurs.

    Args:
        y0 (np.ndarray): initial state.
        t_span (list): [t0, t_upper_limit].
        partition (Partition): partition.stochastic and partition.deterministic are masks for pathways.
        k (f: float -> np.ndarray or np.ndarray): either a callable that gives rate constants at time t or a list of unchanging rate constants.
        N (np.ndarray): the stoichiometry matrix for the system. N_ij = net change in i after unit progress in reaction j.
        rate_involvement_matrix (np.ndarray): A_ij = kinetic order for species i in reaction j.
        rng (np.random.Generator)
        events (list[Callable[[np.ndarray], float]]): a list of continuous functions of the state that have a 0 when an event of interest occurs.
        simulation_options (SimulationOptions): configuration of the simulation.

    Returns:
        StepUpdate: update object:
            update.t: time at end of step,
            update.y: state at end of step,
            update.status: -1 if integration error, 0 if upper limit, 1 if stoch event, 2 if other event,
            update.nfev: number of evaluations of derivative.
    """
    event_flag = False

    # not really a hitting *time* as this is a dimensionless quantity,
    # a random number drawn from the unit exponential distribution.
    # when the integral of the stochastic rates == hitting_point, an event occurs
    hitting_point = rng.exponential(1)

    extra_events = events
    # a continuous event function that will record 0 when the hitting point is reached
    events = [stochastic_event_finder]
    events.extend(extra_events)
    # all our events should be terminal
    for e in events:
        assert e.terminal

    # we have an extra entry in our state vector to carry the integral of the rates of stochastic events, which dictates when an event fires
    y0_expanded = np.zeros(len(y0)+1)
    y0_expanded[:-1] = y0

    # integrate until hitting or until t_max
    step_solved = solve_ivp(dydt, t_span, y0_expanded, events=events, args=(k, partition.deterministic, partition.stochastic, hitting_point))

    # remove the integral of the rates, which is slotted into the last entry of y
    y_all = step_solved.y[:-1,:]
    y_last = y_all[:,-1]

    # if no event occurred, simply return the current values of t and y
    if step_solved.status == -1:
        assert False, "integration step failed"
    elif step_solved.status == 0:
        y_last = round_with_method(y_last, simulation_options.round_randomly, rng)
        return StepUpdate(step_solved.t[-1], y_last, StepStatus.upper_limit, step_solved.nfev, step_solved.t, y_all)

    #print("t_events:", step_solved.t_events)
    # if we reach here, an event has occured
    assert step_solved.status == 1
    # if an event occured, move to that time point
    # first ensure that our expectations are met: 1 event of 1 kind, because we insist on terminal events
    t, y, event_index = canonicalize_event(step_solved.t_events, step_solved.y_events)
    # we constructed the events list so that the 0th element is always the occurrence of a stochastic event
    if event_index == 0:
        event_flag = True

    # drop expanded term for sum of rates
    y = y[:-1]
    # round
    y = round_with_method(y, simulation_options.round_randomly, rng)

    # if the event isn't a stochastic event, then we were halting to reassess partition
    # so: we simply return the state of the system at the time of our event
    if not event_flag:
        return StepUpdate(t, y, StepStatus.other_terminal_event, step_solved.nfev, step_solved.t, y_all)

    # if the event was a stochastic event, cause it to happen
    # first by determining which event happened
    endpoint_propensities = calculate_propensities(y, k, t)

    # OPEN QUESTION: should we recalculate endpoint partition or should we use current partition?
    # I think probably just use current partition!
    #endpoint_partition = partition_function(endpoint_propensities)
    # I think we want to use starting partition but endpoint propensities!

    valid_selections = endpoint_propensities * partition.stochastic

    # TODO: fix if sum == 0 to have no division by 0. WHY DOES THIS HAPPEN?
    selection_sum = valid_selections.sum()
    assert selection_sum != 0. 
    valid_selections /= selection_sum
    valid_selections = valid_selections.cumsum()

    # the first entry that is greater than a random float is our event choice
    pathway_rand = rng.random()
    #print(valid_selections > pathway_rand)
    entry = np.argmax(valid_selections > pathway_rand)
    path_index = np.unravel_index(entry, valid_selections.shape)

    # N_ij = net change in i after unit progress in reaction j
    # so the appropriate column of the stoich matrix tells us how to do our update
    update = np.transpose(N[:,path_index])
    update = update.reshape(y.shape)
    y += update
    #print("stochastic event index", path_index, "update", update)
    #print(y)
    return StepUpdate(t,y,StepStatus.stochastic_event,step_solved.nfev, step_solved.t, y_all)

def forward_time(y0: np.ndarray, t_span: list[float], k: Callable[[float], np.ndarray], N: np.ndarray, rate_involvement_matrix: np.ndarray, rng: np.random.Generator, partition_function: Callable[[np.ndarray], Partition] = None, discontinuities=[], events=[], expert_dydt_factory=None, **kwargs) -> SimulationResult:
    """Evolve system of irreversible reactions forward in time using hybrid deterministic-stochastic approximation.

    Args:
        y0 (np.ndarray): initial state of the system.
        t_span (list[float]): [t0, t_upper_limit].
        k (f: float -> np.ndarray or np.ndarray): either a callable that gives rate constants at time t or an array of unchanging rate constants.
        N (np.ndarray): the stoichiometry matrix for the system. N_ij = net change in i after unit progress in reaction j.
        rate_involvement_matrix (np.ndarray): A_ij = kinetic intensity (power) for species i in reaction j.
        rng (np.random.Generator): rng to use for stochastic simulation (and rounding).
        partition_function (Callable[[np.ndarray], Partition]): function that takes rates at time t and outputs a partition of the system. Optional, but will give error if not specified.
        discontinuities (list[float], optional): a list of time points where k(t) is discontinuous. Defaults to [].
        events (list[Callable[[np.ndarray], float]], optional): a list of continuous functions of the state that have a 0 when an event of interest occurs. Defaults to [].
        expert_dydt_factory (Callable[[Callable], Callable]): a function that takes the propensity calculator and returns dydt. This interface is useful if through expert knowledge of the system, you can provide a faster calculation of the derivative than the matrix multiplication rates = N @ deterministic_propensities. Defaults to None.
        **kwargs (SimulationOptions): configuration of the simulation. Defaults to SimulationOptions().

    Returns:
        SimulationResult: simulation result object:
            result.t: t_end, should be close to t_span[-1],
            result.y: system state at t_end,
            result.n_stochastic: number of stochastic events that occured,
            result.nfev: number of evaluations of the derivative.
            result.t_history: every time point evaluated.
            result.y_history: state of the system at each point in t_history.

    """
    if isinstance(k, str):
        raise TypeError(f"Instead of a function or matrix, found this message for k: {k}")
    if partition_function is None:
        raise TypeError("partition function must be specified.")
    simulation_options = SimulationOptions(**kwargs)
    if simulation_options.jit:
        calculate_propensities = jit_calculate_propensities_factory(rate_involvement_matrix.astype(np.float64))
    else:
        calculate_propensities = calculate_propensities_factory(rate_involvement_matrix)

    if expert_dydt_factory is None:
        if simulation_options.jit:
            dydt = jit_dydt_factory(N.astype(np.float64), calculate_propensities)
        else:
            dydt = dydt_factory(N, calculate_propensities)
    else:
        dydt = expert_dydt_factory(calculate_propensities)
        if simulation_options.jit:
            from numba.core.registry import CPUDispatcher
            assert(isinstance(dydt, CPUDispatcher))

    discontinuities = np.sort(np.array(discontinuities))
    # ignore a discontinuity at 0
    if len(discontinuities) > 0 and discontinuities[0] == 0:
        discontinuities = discontinuities[1:]
    n_stochastic = 0
    nfev = 0
    t0, t_end = t_span
    t = t0
    y = y0

    history_length = int(1e6)
    t_history = np.zeros(history_length)
    y_history = np.zeros((len(y), history_length))
    history_index = 0

    next_discontinuity_index = 0
    discontinuity_surgery_flag = False
    while t < t_end:
        if discontinuity_surgery_flag and t < discontinuities[next_discontinuity_index-1]:
            discontinuity_surgery_flag = False
            print(f"Jumping from {t} to {np.nextafter(discontinuities[next_discontinuity_index-1], t_end)} to avoid discontinuity")
            t = np.nextafter(discontinuities[next_discontinuity_index-1], t_end)


        propensities = calculate_propensities(y, k, t)
        if next_discontinuity_index < len(discontinuities):
            # a double less than the discontinuity --- we don't want to "look ahead" to the point of the discontinuity
            upper_limit = np.nextafter(discontinuities[next_discontinuity_index], t0)
            if upper_limit == discontinuities[next_discontinuity_index]:
                import pdb; pdb.set_trace()
        else:
            upper_limit = t_end
        partition = partition_function(propensities)

        _y_expanded = np.zeros(len(y)+1)
        _y_expanded[:-1] = y
        #print("propensities:", propensities)
        #print("dydt:", dydt(t, _y_expanded, k_of_t, N, rate_involvement_matrix, partition, 0))

        step_update = hybrid_step(calculate_propensities, dydt, y, [t, upper_limit], partition, k, N, rate_involvement_matrix, rng, events, simulation_options)

        t = step_update.t
        y = step_update.y
        if step_update.status == StepStatus.stochastic_event:
            n_stochastic += 1
        elif step_update.status == StepStatus.upper_limit:
            discontinuity_surgery_flag = True
            next_discontinuity_index += 1
        nfev += step_update.nfev

        n_samples = len(step_update.t_history)
        t_history[history_index:history_index+n_samples] = step_update.t_history
        y_history[:, history_index:history_index+n_samples] = step_update.y_history
        history_index += n_samples

    # truncate histories
    t_history = t_history[:history_index]
    y_history = y_history[:, :history_index]
    return SimulationResult(t,y,n_stochastic,nfev,t_history,y_history)