'''!
 * Copyright (c) 2020-2021 Microsoft Corporation. All rights reserved.
 * Licensed under the MIT License. See LICENSE file in the
 * project root for license information.
'''
from typing import Dict, Optional, List, Tuple, Callable
import numpy as np
import time
import pickle

try:
    from ray.tune.suggest import Searcher
    from ray.tune.suggest.optuna import OptunaSearch as GlobalSearch
    from ray.tune.suggest.variant_generator import generate_variants
except ImportError:
    from .suggestion import Searcher
    from .suggestion import OptunaSearch as GlobalSearch
    from .variant_generator import generate_variants
from .search_thread import SearchThread
from .flow2 import FLOW2

import logging
logger = logging.getLogger(__name__)


class BlendSearch(Searcher):
    '''class for BlendSearch algorithm
    '''

    cost_attr = "time_total_s"  # cost attribute in result
    lagrange = '_lagrange'      # suffix for lagrange-modified metric
    penalty = 1e+10             # penalty term for constraints
    LocalSearch = FLOW2

    def __init__(self,
                 metric: Optional[str] = None,
                 mode: Optional[str] = None,
                 space: Optional[dict] = None,
                 points_to_evaluate: Optional[List[dict]] = None,
                 low_cost_partial_config: Optional[dict] = None,
                 cat_hp_cost: Optional[dict] = None,
                 prune_attr: Optional[str] = None,
                 min_resource: Optional[float] = None,
                 max_resource: Optional[float] = None,
                 reduction_factor: Optional[float] = None,
                 global_search_alg: Optional[Searcher] = None,
                 config_constraints: Optional[
                     List[Tuple[Callable[[dict], float], str, float]]] = None,
                 metric_constraints: Optional[
                     List[Tuple[str, str, float]]] = None,
                 seed: Optional[int] = 20):
        '''Constructor

        Args:
            metric: A string of the metric name to optimize for.
            mode: A string in ['min', 'max'] to specify the objective as
                minimization or maximization.
            space: A dictionary to specify the search space.
            points_to_evaluate: Initial parameter suggestions to be run first.
            low_cost_partial_config: A dictionary from a subset of
                controlled dimensions to the initial low-cost values.
                e.g.,

                .. code-block:: python

                    {'n_estimators': 4, 'max_leaves': 4}

            cat_hp_cost: A dictionary from a subset of categorical dimensions
                to the relative cost of each choice.
                e.g.,

                .. code-block:: python

                    {'tree_method': [1, 1, 2]}

                i.e., the relative cost of the
                three choices of 'tree_method' is 1, 1 and 2 respectively.
            prune_attr: A string of the attribute used for pruning.
                Not necessarily in space.
                When prune_attr is in space, it is a hyperparameter, e.g.,
                    'n_iters', and the best value is unknown.
                When prune_attr is not in space, it is a resource dimension,
                    e.g., 'sample_size', and the peak performance is assumed
                    to be at the max_resource.
            min_resource: A float of the minimal resource to use for the
                prune_attr; only valid if prune_attr is not in space.
            max_resource: A float of the maximal resource to use for the
                prune_attr; only valid if prune_attr is not in space.
            reduction_factor: A float of the reduction factor used for
                incremental pruning.
            global_search_alg: A Searcher instance as the global search
                instance. If omitted, Optuna is used. The following algos have
                known issues when used as global_search_alg:
                - HyperOptSearch raises exception sometimes
                - TuneBOHB has its own scheduler
            config_constraints: A list of config constraints to be satisfied.
                e.g.,

                .. code-block: python

                    config_constraints = [(mem_size, '<=', 1024**3)]

                mem_size is a function which produces a float number for the bytes
                needed for a config.
                It is used to skip configs which do not fit in memory.
            metric_constraints: A list of metric constraints to be satisfied.
                e.g., `['precision', '>=', 0.9]`
            seed: An integer of the random seed.
        '''
        self._metric, self._mode = metric, mode
        init_config = low_cost_partial_config or {}
        if not init_config:
            logger.warning(
                "No low-cost partial config given to the search algorithm. "
                "For cost-frugal search, "
                "consider providing low-cost values for cost-related hps via "
                "'low_cost_partial_config'."
            )
        self._points_to_evaluate = points_to_evaluate or []
        self._config_constraints = config_constraints
        self._metric_constraints = metric_constraints
        if self._metric_constraints:
            # metric modified by lagrange
            metric += self.lagrange
        if global_search_alg is not None:
            self._gs = global_search_alg
        elif getattr(self, '__name__', None) != 'CFO':
            try:
                gs_seed = seed - 10 if (seed - 10) >= 0 else seed - 11 + (1 << 32)
                self._gs = GlobalSearch(space=space, metric=metric, mode=mode, seed=gs_seed)
            except TypeError:
                self._gs = GlobalSearch(space=space, metric=metric, mode=mode)
        else:
            self._gs = None
        if getattr(self, '__name__', None) == 'CFO' and points_to_evaluate and len(
           points_to_evaluate) > 1:
            # use the best config in points_to_evaluate as the start point
            self._candidate_start_points = {}
            self._started_from_low_cost = not low_cost_partial_config
        else:
            self._candidate_start_points = None
        self._ls = self.LocalSearch(
            init_config, metric, mode, cat_hp_cost, space, prune_attr,
            min_resource, max_resource, reduction_factor, self.cost_attr, seed)
        self._init_search()

    def set_search_properties(self,
                              metric: Optional[str] = None,
                              mode: Optional[str] = None,
                              config: Optional[Dict] = None) -> bool:
        metric_changed = mode_changed = False
        if metric and self._metric != metric:
            metric_changed = True
            self._metric = metric
            if self._metric_constraints:
                # metric modified by lagrange
                metric += self.lagrange
                # TODO: don't change metric for global search methods that
                # can handle constraints already
        if mode and self._mode != mode:
            mode_changed = True
            self._mode = mode
        if not self._ls.space:
            # the search space can be set only once
            self._ls.set_search_properties(metric, mode, config)
            if self._gs is not None:
                self._gs.set_search_properties(metric, mode, config)
            self._init_search()
        elif metric_changed or mode_changed:
            # reset search when metric or mode changed
            self._ls.set_search_properties(metric, mode)
            if self._gs is not None:
                self._gs.set_search_properties(metric, mode)
            self._init_search()
        if config:
            if 'time_budget_s' in config:
                time_budget_s = config['time_budget_s']
                if time_budget_s is not None:
                    self._deadline = time_budget_s + time.time()
                    SearchThread.set_eps(time_budget_s)
            if 'metric_target' in config:
                self._metric_target = config.get('metric_target')
        return True

    def _init_search(self):
        '''initialize the search
        '''
        self._metric_target = np.inf * self._ls.metric_op
        self._search_thread_pool = {
            # id: int -> thread: SearchThread
            0: SearchThread(self._ls.mode, self._gs)
        }
        self._thread_count = 1  # total # threads created
        self._init_used = self._ls.init_config is None
        self._trial_proposed_by = {}  # trial_id: str -> thread_id: int
        self._ls_bound_min = self._ls.normalize(self._ls.init_config)
        self._ls_bound_max = self._ls_bound_min.copy()
        self._gs_admissible_min = self._ls_bound_min.copy()
        self._gs_admissible_max = self._ls_bound_max.copy()
        self._result = {}  # config_signature: tuple -> result: Dict
        self._deadline = np.inf
        if self._metric_constraints:
            self._metric_constraint_satisfied = False
            self._metric_constraint_penalty = [
                self.penalty for _ in self._metric_constraints]
        else:
            self._metric_constraint_satisfied = True
            self._metric_constraint_penalty = None

    def save(self, checkpoint_path: str):
        ''' save states to a checkpoint path
        '''
        save_object = self
        with open(checkpoint_path, "wb") as outputFile:
            pickle.dump(save_object, outputFile)

    def restore(self, checkpoint_path: str):
        ''' restore states from checkpoint
        '''
        with open(checkpoint_path, "rb") as inputFile:
            state = pickle.load(inputFile)
        self._metric_target = state._metric_target
        self._search_thread_pool = state._search_thread_pool
        self._thread_count = state._thread_count
        self._init_used = state._init_used
        self._trial_proposed_by = state._trial_proposed_by
        self._ls_bound_min = state._ls_bound_min
        self._ls_bound_max = state._ls_bound_max
        self._gs_admissible_min = state._gs_admissible_min
        self._gs_admissible_max = state._gs_admissible_max
        self._result = state._result
        self._deadline = state._deadline
        self._metric, self._mode = state._metric, state._mode
        self._points_to_evaluate = state._points_to_evaluate
        self._gs = state._gs
        self._ls = state._ls
        self._config_constraints = state._config_constraints
        self._metric_constraints = state._metric_constraints
        self._metric_constraint_satisfied = state._metric_constraint_satisfied
        self._metric_constraint_penalty = state._metric_constraint_penalty
        self._candidate_start_points = state._candidate_start_points
        if self._candidate_start_points:
            self._started_from_given = state._started_from_given
            self._started_from_low_cost = state._started_from_low_cost

    @property
    def metric_target(self):
        return self._metric_target

    def on_trial_complete(self, trial_id: str, result: Optional[Dict] = None,
                          error: bool = False):
        ''' search thread updater and cleaner
        '''
        metric_constraint_satisfied = True
        if result and not error and self._metric_constraints:
            # account for metric constraints if any
            objective = result[self._metric]
            for i, constraint in enumerate(self._metric_constraints):
                metric_constraint, sign, threshold = constraint
                value = result.get(metric_constraint)
                if value:
                    # sign is <= or >=
                    sign_op = 1 if sign == '<=' else -1
                    violation = (value - threshold) * sign_op
                    if violation > 0:
                        # add penalty term to the metric
                        objective += self._metric_constraint_penalty[
                            i] * violation * self._ls.metric_op
                        metric_constraint_satisfied = False
                        if self._metric_constraint_penalty[i] < self.penalty:
                            self._metric_constraint_penalty[i] += violation
            result[self._metric + self.lagrange] = objective
            if metric_constraint_satisfied and not self._metric_constraint_satisfied:
                # found a feasible point
                self._metric_constraint_penalty = [1 for _ in self._metric_constraints]
            self._metric_constraint_satisfied |= metric_constraint_satisfied
        thread_id = self._trial_proposed_by.get(trial_id)
        if thread_id in self._search_thread_pool:
            self._search_thread_pool[thread_id].on_trial_complete(
                trial_id, result, error)
            del self._trial_proposed_by[trial_id]
        if result:
            config = {}
            for key, value in result.items():
                if key.startswith('config/'):
                    config[key[7:]] = value
            if error:  # remove from result cache
                del self._result[self._ls.config_signature(config)]
            else:  # add to result cache
                self._result[self._ls.config_signature(config)] = result
                # update target metric if improved
                objective = result[self._ls.metric]
                if (objective - self._metric_target) * self._ls.metric_op < 0:
                    self._metric_target = objective
                if thread_id == 0 and metric_constraint_satisfied \
                   and self._create_condition(result):
                    # thread creator
                    thread_id = self._thread_count
                    self._started_from_given = self._candidate_start_points \
                        and trial_id in self._candidate_start_points
                    if self._started_from_given:
                        del self._candidate_start_points[trial_id]
                    else:
                        self._started_from_low_cost = True
                    self._create_thread(config, result)
                elif thread_id and not self._metric_constraint_satisfied:
                    # no point has been found to satisfy metric constraint
                    self._expand_admissible_region()
                # reset admissible region to ls bounding box
                self._gs_admissible_min.update(self._ls_bound_min)
                self._gs_admissible_max.update(self._ls_bound_max)
        # cleaner
        if thread_id and thread_id in self._search_thread_pool:
            # local search thread
            self._clean(thread_id)

    def _create_thread(self, config, result):
        # logger.info(f"create local search thread from {config}")
        self._search_thread_pool[self._thread_count] = SearchThread(
            self._ls.mode,
            self._ls.create(
                config, result[self._ls.metric],
                cost=result.get(self.cost_attr, 1)),
            self.cost_attr
        )
        self._thread_count += 1
        self._update_admissible_region(
            config, self._ls_bound_min, self._ls_bound_max)

    def _update_admissible_region(self, config, admissible_min, admissible_max):
        # update admissible region
        normalized_config = self._ls.normalize(config)
        for key in admissible_min:
            value = normalized_config[key]
            if value > admissible_max[key]:
                admissible_max[key] = value
            elif value < admissible_min[key]:
                admissible_min[key] = value

    def _create_condition(self, result: Dict) -> bool:
        ''' create thread condition
        '''
        if len(self._search_thread_pool) < 2:
            return True
        obj_median = np.median(
            [thread.obj_best1 for id, thread in self._search_thread_pool.items()
             if id])
        return result[self._ls.metric] * self._ls.metric_op < obj_median

    def _clean(self, thread_id: int):
        ''' delete thread and increase admissible region if converged,
        merge local threads if they are close
        '''
        assert thread_id
        todelete = set()
        for id in self._search_thread_pool:
            if id and id != thread_id:
                if self._inferior(id, thread_id):
                    todelete.add(id)
        for id in self._search_thread_pool:
            if id and id != thread_id:
                if self._inferior(thread_id, id):
                    todelete.add(thread_id)
                    break
        create_new = False
        if self._search_thread_pool[thread_id].converged:
            todelete.add(thread_id)
            self._expand_admissible_region()
            if self._candidate_start_points:
                if not self._started_from_given:
                    # remove start points whose perf is worse than the converged
                    obj = self._search_thread_pool[thread_id].obj_best1
                    worse = [
                        trial_id
                        for trial_id, r in self._candidate_start_points.items()
                        if r and r[self._ls.metric] * self._ls.metric_op >= obj]
                    # logger.info(f"remove candidate start points {worse} than {obj}")
                    for trial_id in worse:
                        del self._candidate_start_points[trial_id]
                if self._candidate_start_points and self._started_from_low_cost:
                    create_new = True
        for id in todelete:
            del self._search_thread_pool[id]
        if create_new:
            self._create_thread_from_best_candidate()

    def _create_thread_from_best_candidate(self):
        # find the best start point
        best_trial_id = None
        obj_best = None
        for trial_id, r in self._candidate_start_points.items():
            if r and (best_trial_id is None
                      or r[self._ls.metric] * self._ls.metric_op < obj_best):
                best_trial_id = trial_id
                obj_best = r[self._ls.metric] * self._ls.metric_op
        if best_trial_id:
            # create a new thread
            config = {}
            result = self._candidate_start_points[best_trial_id]
            for key, value in result.items():
                if key.startswith('config/'):
                    config[key[7:]] = value
            self._started_from_given = True
            del self._candidate_start_points[best_trial_id]
            self._create_thread(config, result)

    def _expand_admissible_region(self):
        for key in self._ls_bound_max:
            self._ls_bound_max[key] += self._ls.STEPSIZE
            self._ls_bound_min[key] -= self._ls.STEPSIZE

    def _inferior(self, id1: int, id2: int) -> bool:
        ''' whether thread id1 is inferior to id2
        '''
        t1 = self._search_thread_pool[id1]
        t2 = self._search_thread_pool[id2]
        if t1.obj_best1 < t2.obj_best2:
            return False
        elif t1.resource and t1.resource < t2.resource:
            return False
        elif t2.reach(t1):
            return True
        return False

    def on_trial_result(self, trial_id: str, result: Dict):
        ''' receive intermediate result
        '''
        if trial_id not in self._trial_proposed_by:
            return
        thread_id = self._trial_proposed_by[trial_id]
        if thread_id not in self._search_thread_pool:
            return
        if result and self._metric_constraints:
            result[self._metric + self.lagrange] = result[self._metric]
        self._search_thread_pool[thread_id].on_trial_result(trial_id, result)

    def suggest(self, trial_id: str) -> Optional[Dict]:
        ''' choose thread, suggest a valid config
        '''
        if self._init_used and not self._points_to_evaluate:
            choice, backup = self._select_thread()
            if choice < 0:  # timeout
                return None
            config = self._search_thread_pool[choice].suggest(trial_id)
            if choice and config is None:
                # local search thread finishes
                if self._search_thread_pool[choice].converged:
                    self._expand_admissible_region()
                    del self._search_thread_pool[choice]
                return None
            # preliminary check; not checking config validation
            skip = self._should_skip(choice, trial_id, config)
            if skip:
                if choice:
                    return None
                # use rs when BO fails to suggest a config
                for _, generated in generate_variants({'config': self._ls.space}):
                    config = generated['config']
                    break  # get one random config
                skip = self._should_skip(-1, trial_id, config)
                if skip:
                    return None
            if choice or self._valid(config):
                # LS or valid or no backup choice
                self._trial_proposed_by[trial_id] = choice
            else:  # invalid config proposed by GS
                if choice == backup:
                    # use CFO's init point
                    init_config = self._ls.init_config
                    config = self._ls.complete_config(
                        init_config, self._ls_bound_min, self._ls_bound_max)
                    self._trial_proposed_by[trial_id] = choice
                else:
                    config = self._search_thread_pool[backup].suggest(trial_id)
                    skip = self._should_skip(backup, trial_id, config)
                    if skip:
                        return None
                    self._trial_proposed_by[trial_id] = backup
                    choice = backup
            if not choice:  # global search
                if self._ls._resource:
                    # TODO: min or median?
                    config[self._ls.prune_attr] = self._ls.min_resource
                # temporarily relax admissible region for parallel proposals
                self._update_admissible_region(
                    config, self._gs_admissible_min, self._gs_admissible_max)
            else:
                self._update_admissible_region(
                    config, self._ls_bound_min, self._ls_bound_max)
                self._gs_admissible_min.update(self._ls_bound_min)
                self._gs_admissible_max.update(self._ls_bound_max)
            self._result[self._ls.config_signature(config)] = {}
        else:  # use init config
            if self._candidate_start_points is not None and self._points_to_evaluate:
                self._candidate_start_points[trial_id] = None
            init_config = self._points_to_evaluate.pop(
                0) if self._points_to_evaluate else self._ls.init_config
            config = self._ls.complete_config(
                init_config, self._ls_bound_min, self._ls_bound_max)
            config_signature = self._ls.config_signature(config)
            result = self._result.get(config_signature)
            if result:  # tried before
                return None
            elif result is None:  # not tried before
                self._result[config_signature] = {}
            else:  # running but no result yet
                return None
            self._init_used = True
            self._trial_proposed_by[trial_id] = 0
            self._search_thread_pool[0].running += 1
        return config

    def _should_skip(self, choice, trial_id, config) -> bool:
        ''' if config is None or config's result is known or constraints are violated
            return True; o.w. return False
        '''
        if config is None:
            return True
        config_signature = self._ls.config_signature(config)
        exists = config_signature in self._result
        # check constraints
        if not exists and self._config_constraints:
            for constraint in self._config_constraints:
                func, sign, threshold = constraint
                value = func(config)
                if (sign == '<=' and value > threshold
                        or sign == '>=' and value < threshold):
                    self._result[config_signature] = {
                        self._metric: np.inf * self._ls.metric_op,
                        'time_total_s': 1,
                    }
                    exists = True
                    break
        if exists:  # suggested before
            if choice >= 0:  # not fallback to rs
                result = self._result.get(config_signature)
                if result:  # finished
                    self._search_thread_pool[choice].on_trial_complete(
                        trial_id, result, error=False)
                    if choice:
                        # local search thread
                        self._clean(choice)
                # else:     # running
                #     # tell the thread there is an error
                #     self._search_thread_pool[choice].on_trial_complete(
                #         trial_id, {}, error=True)
            return True
        return False

    def _select_thread(self) -> Tuple:
        ''' thread selector; use can_suggest to check LS availability
        '''
        # update priority
        min_eci = self._deadline - time.time()
        if min_eci <= 0:
            # return -1, -1
            # keep proposing new configs assuming no budget left
            min_eci = 0
        max_speed = 0
        for thread in self._search_thread_pool.values():
            if thread.speed > max_speed:
                max_speed = thread.speed
        for thread in self._search_thread_pool.values():
            thread.update_eci(self._metric_target, max_speed)
            if thread.eci < min_eci:
                min_eci = thread.eci
        for thread in self._search_thread_pool.values():
            thread.update_priority(min_eci)

        top_thread_id = backup_thread_id = 0
        priority1 = priority2 = self._search_thread_pool[0].priority
        for thread_id, thread in self._search_thread_pool.items():
            # if thread_id:
            #     print(
            #         f"priority of thread {thread_id}={thread.priority}")
            #     logger.debug(
            #         f"thread {thread_id}.can_suggest={thread.can_suggest}")
            if thread_id and thread.can_suggest:
                priority = thread.priority
                if priority > priority1:
                    priority1 = priority
                    top_thread_id = thread_id
                if priority > priority2 or backup_thread_id == 0:
                    priority2 = priority
                    backup_thread_id = thread_id
        return top_thread_id, backup_thread_id

    def _valid(self, config: Dict) -> bool:
        ''' config validator
        '''
        normalized_config = self._ls.normalize(config)
        for key in self._gs_admissible_min:
            if key in config:
                value = normalized_config[key]
                if value + self._ls.STEPSIZE < self._gs_admissible_min[key] \
                        or value > self._gs_admissible_max[key] + self._ls.STEPSIZE:
                    return False
        return True


try:
    from ray.tune import (uniform, quniform, choice, randint, qrandint, randn,
                          qrandn, loguniform, qloguniform)
except ImportError:
    from ..tune.sample import (uniform, quniform, choice, randint, qrandint, randn,
                               qrandn, loguniform, qloguniform)

try:
    from nni.tuner import Tuner as NNITuner
    from nni.utils import extract_scalar_reward

    class BlendSearchTuner(BlendSearch, NNITuner):
        '''Tuner class for NNI
        '''

        def receive_trial_result(self, parameter_id, parameters, value,
                                 **kwargs):
            '''
            Receive trial's final result.
            parameter_id: int
            parameters: object created by 'generate_parameters()'
            value: final metrics of the trial, including default metric
            '''
            result = {}
            for key, value in parameters.items():
                result['config/' + key] = value
            reward = extract_scalar_reward(value)
            result[self._metric] = reward
            # if nni does not report training cost,
            # using sequence as an approximation.
            # if no sequence, using a constant 1
            result[self.cost_attr] = value.get(self.cost_attr, value.get(
                'sequence', 1))
            self.on_trial_complete(str(parameter_id), result)
        ...

        def generate_parameters(self, parameter_id, **kwargs) -> Dict:
            '''
            Returns a set of trial (hyper-)parameters, as a serializable object
            parameter_id: int
            '''
            return self.suggest(str(parameter_id))
        ...

        def update_search_space(self, search_space):
            '''
            Tuners are advised to support updating search space at run-time.
            If a tuner can only set search space once before generating first hyper-parameters,
            it should explicitly document this behaviour.
            search_space: JSON object created by experiment owner
            '''
            config = {}
            for key, value in search_space.items():
                v = value.get("_value")
                _type = value['_type']
                if _type == 'choice':
                    config[key] = choice(v)
                elif _type == 'randint':
                    config[key] = randint(v[0], v[1] - 1)
                elif _type == 'uniform':
                    config[key] = uniform(v[0], v[1])
                elif _type == 'quniform':
                    config[key] = quniform(v[0], v[1], v[2])
                elif _type == 'loguniform':
                    config[key] = loguniform(v[0], v[1])
                elif _type == 'qloguniform':
                    config[key] = qloguniform(v[0], v[1], v[2])
                elif _type == 'normal':
                    config[key] = randn(v[1], v[2])
                elif _type == 'qnormal':
                    config[key] = qrandn(v[1], v[2], v[3])
                else:
                    raise ValueError(
                        f'unsupported type in search_space {_type}')
            self._ls.set_search_properties(None, None, config)
            if self._gs is not None:
                self._gs.set_search_properties(None, None, config)
            self._init_search()

except ImportError:
    class BlendSearchTuner(BlendSearch):
        pass


class CFO(BlendSearchTuner):
    ''' class for CFO algorithm
    '''

    __name__ = 'CFO'

    def suggest(self, trial_id: str) -> Optional[Dict]:
        # Number of threads is 1 or 2. Thread 0 is a vacuous thread
        assert len(self._search_thread_pool) < 3, len(self._search_thread_pool)
        if len(self._search_thread_pool) < 2:
            # When a local thread converges, the number of threads is 1
            # Need to restart
            self._init_used = False
        return super().suggest(trial_id)

    def _select_thread(self) -> Tuple:
        for key in self._search_thread_pool:
            if key:
                return key, key

    def _create_condition(self, result: Dict) -> bool:
        ''' create thread condition
        '''
        if self._points_to_evaluate:
            # still evaluating user-specified init points
            # we evaluate all candidate start points before we
            # create the first local search thread
            return False
        if len(self._search_thread_pool) == 2:
            return False
        if self._candidate_start_points and self._thread_count == 1:
            # result needs to match or exceed the best candidate start point
            obj_best = min(
                self._ls.metric_op * r[self._ls.metric]
                for r in self._candidate_start_points.values() if r)
            return result[self._ls.metric] * self._ls.metric_op <= obj_best
        else:
            return True

    def on_trial_complete(self, trial_id: str, result: Optional[Dict] = None,
                          error: bool = False):
        super().on_trial_complete(trial_id, result, error)
        if self._candidate_start_points \
           and trial_id in self._candidate_start_points:
            # the trial is a candidate start point
            self._candidate_start_points[trial_id] = result
            if len(self._search_thread_pool) < 2 and not self._points_to_evaluate:
                self._create_thread_from_best_candidate()
