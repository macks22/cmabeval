import os
import logging
from concurrent import futures

import numpy as np
import pandas as pd
from scipy import special as sps
import matplotlib.pyplot as plt

logger = logging.getLogger(__name__)


def plot_cum_regret(rewards, optimal_rewards, ax=None, **kwargs):
    if ax is None:
        fig, ax = plt.subplots(figsize=kwargs.pop('figsize', None))

    regret = optimal_rewards - rewards
    cum_regret = np.cumsum(regret, axis=-1)
    pd.DataFrame(cum_regret.T).plot(
        ax=ax,
        color=kwargs.get('color', 'red'),
        alpha=kwargs.get('alpha', 0.5))

    fontsize = kwargs.pop('fontsize', 14)
    ax.set_ylabel('Cumulative Regret', fontsize=fontsize)
    ax.set_xlabel('Trial Number', fontsize=fontsize)
    ax.get_legend().remove()
    ax.set_title(kwargs.get('title', ''), fontsize=fontsize + 2)

    return ax


# TODO: import from common base module
class Seedable:
    """Inherit from this class to get methods useful for objects
    using random seeds.
    """
    def __init__(self, seed=42):
        self._initial_seed = seed
        self.rng = np.random.RandomState(self._initial_seed)

    def seed(self, seed):
        self.rng.seed(seed)
        return self

    def reset(self):
        self.seed(self._initial_seed)
        return self


class GaussianSimulationFactory(Seedable):
    """Simulate data according to contextual Gaussian distributions.

    A factory creates individual environments.
    This particular factory creates `GaussianSimulationEnvironment`s.
    """

    def __init__(self, num_arms=100, num_predictors=10, num_time_steps=1000,
                 *, prior_effect_means=None, prior_effect_cov=None,
                 prior_context_means=None, prior_context_cov=None, **kwargs):
        super().__init__(**kwargs)

        self.num_arms = num_arms
        self.num_predictors = num_predictors
        self.num_time_steps = num_time_steps

        # Set prior parameters for effects
        self.prior_effect_means = prior_effect_means
        if self.prior_effect_means is None:
            self.prior_effect_means = np.zeros(
                self.num_predictors, dtype=np.float)

        self.prior_effect_cov = prior_effect_cov
        if self.prior_effect_cov is None:
            self.prior_effect_cov = np.identity(
                self.num_predictors, dtype=np.float)

        # Set prior parameters for arm contexts
        self.prior_context_means = prior_context_means
        if self.prior_context_means is None:
            self.prior_context_means = np.ones(self.num_predictors, dtype=np.float) * -3

        self.prior_context_cov = prior_context_cov
        if self.prior_context_cov is None:
            self.prior_context_cov = np.identity(self.num_predictors, dtype=np.float)

    def __call__(self):
        # Generate true effects
        true_effects = self.rng.multivariate_normal(
            self.prior_effect_means, self.prior_effect_cov)
        logger.info(f'True effects: {np.round(true_effects, 4)}')

        # Generate design matrix
        arm_contexts = self.rng.multivariate_normal(
            self.prior_context_means, self.prior_context_cov, size=self.num_arms)
        logger.info(f'Context matrix size: {arm_contexts.shape}')

        return GaussianSimulationEnvironment(
            true_effects, arm_contexts, seed=self.rng.randint(0, 2**32))


class GaussianSimulationEnvironment(Seedable):
    """An environment with Gaussian-distributed rewards related to
    contextual covariates linearly through a logistic link function.

    To replicate an experiment with the same environment but different
    random seeds, simply change the random seed after the first experiment
    is complete. If running in parallel, create multiple of these objects
    with different random seeds but the same parameters otherwise.
    """

    def __init__(self, true_effects, arm_contexts, **kwargs):
        super().__init__(**kwargs)

        self.true_effects = true_effects
        self.arm_contexts = arm_contexts
        self.arm_rates = self._recompute_arm_rates()
        self.optimal_arm = np.argmax(self.arm_rates)
        self.optimal_rate = self.arm_rates[self.optimal_arm]

    def _recompute_arm_rates(self):
        logits = self.arm_contexts.dot(self.true_effects)
        return sps.expit(logits)

    @property
    def num_arms(self):
        return self.arm_contexts.shape[0]

    @property
    def num_predictors(self):
        return self.arm_contexts.shape[1]

    def __str__(self):
        return (f'{self.__class__.__name__}'
                f', num_predictors={self.num_predictors}'
                f', num_arms={self.num_arms}'
                f', max_arm_rate={np.round(np.max(self.arm_rates), 5)}'
                f', mean_arm_rate={np.round(np.mean(self.arm_rates), 5)}')

    def __repr__(self):
        return self.__str__()

    def random_arm(self):
        return self.rng.choice(self.num_arms)

    def choose_arm(self, i):
        self._validate_arm_index(i)

        # Generate data for optimal arm.
        y_optimal = self.rng.binomial(n=1, p=self.optimal_rate)

        # Generate data for selected arm.
        context = self.arm_contexts[i]
        if i == self.optimal_arm:
            y = y_optimal
        else:
            y = self.rng.binomial(n=1, p=self.arm_rates[i])

        return context, y, y_optimal

    def _validate_arm_index(self, i):
        if i < 0 or i >= self.num_arms:
            raise ValueError(
                f'arm a must satisfy: 0 < a < {self.num_arms}; got {i}')


class SingleScenarioMetrics:
    """Record metrics from a single replication of an experiment."""

    arm_colname = 'arm'
    reward_colname = 'reward'
    optimal_reward_colname = 'optimal_reward'

    def __init__(self, seed, num_time_steps, num_predictors):
        self.seed = seed
        self.design_matrix = np.ndarray((num_time_steps, num_predictors))
        self.rewards = np.ndarray(num_time_steps)
        self.optimal_rewards = np.ndarray(num_time_steps)
        self.arm_selected = np.ndarray(num_time_steps, dtype=np.uint)

    @property
    def num_time_steps(self):
        return self.design_matrix.shape[0]

    @property
    def num_predictors(self):
        return self.design_matrix.shape[1]

    @property
    def predictor_colnames(self):
        return [f'p{i}' for i in range(self.num_predictors)]

    @property
    def colnames(self):
        return self.predictor_colnames + [
            self.arm_colname, self.reward_colname, self.optimal_reward_colname]

    def as_df(self):
        df = pd.DataFrame(index=pd.Index(np.arange(self.num_time_steps), name='time_step'),
                          columns=self.colnames)
        df.loc[:, self.predictor_colnames] = self.design_matrix
        df.loc[:, self.arm_colname] = self.arm_selected
        df.loc[:, self.reward_colname] = self.rewards
        df.loc[:, self.optimal_reward_colname] = self.optimal_rewards
        return df

    @classmethod
    def from_df(cls, df):
        instance = cls(*df.shape)

        instance.design_matrix[:] = df.iloc[:, instance.predictor_colnames]
        instance.arm_selected[:] = df[instance.arm_colname]
        instance.reward_colname[:] = df[instance.reward_colname]
        instance.optimal_rewards[:] = df[instance.optimal_reward_colname]

        return instance

    def save(self, path):
        path = self._standardize_path(path)
        df = self.as_df()
        logger.info(f'saving metrics to {path}')
        df.to_csv(path)

    def _standardize_path(self, path):
        name, ext = os.path.splitext(path)
        return f'{name}_{self.seed}.csv'

    @classmethod
    def load(cls, path):
        df = pd.read_csv(path)
        return cls.from_df(df)


class CrossScenarioMetrics:
    """Record metrics from multiple replications of the same experiment."""

    def __init__(self, metrics=None):
        self._metrics = None
        self._seed_to_replication = None

        # Will build seed to replication mapping
        self.metrics = metrics

    @property
    def metrics(self):
        return self._metrics

    @metrics.setter
    def metrics(self, metrics):
        self._metrics = metrics
        self._rebuild_mapping()

    def append(self, metrics):
        if metrics.seed in self._seed_to_replication:
            logger.warning(f"replacing metrics with seed {metrics.seed}")
            self.remove(metrics.seed)

        self.metrics.append(metrics)
        self._seed_to_replication[metrics.seed] = metrics

    def remove(self, seed_or_metrics):
        if isinstance(seed_or_metrics, SingleScenarioMetrics):
            self.metrics.remove(seed_or_metrics)
        else:
            metrics = self[seed_or_metrics]
            self.metrics.remove(metrics)

    def _rebuild_mapping(self):
        self._seed_to_replication = {m.seed: m for m in self._metrics}

    def __getitem__(self, seed):
        return self._seed_to_replication[seed]

    def save(self, dirpath):
        """Save each metrics object at `dirpath/<seed>.csv`."""
        raise NotImplementedError

    @classmethod
    def load(cls, dirpath):
        raise NotImplementedError


class Experiment(Seedable):
    """Run one or more replicates of agent-environment interaction
    and record the resulting metrics.
    """

    def __init__(self, environment_factory, model,
                 num_time_steps=1000, logging_frequency=100,
                 max_workers=7, **kwargs):
        super().__init__(**kwargs)

        self.environment = environment_factory()
        self.model = model
        self.num_time_steps = num_time_steps
        self.logging_frequency = logging_frequency
        self.max_workers = max_workers

    def run(self, num_replications=1):
        rep_nums = np.arange(num_replications)
        with futures.ProcessPoolExecutor(max_workers=self.max_workers) as pool:
            all_rewards = pool.map(self.run_once, rep_nums)

        rewards, optimal_rewards = list(zip(*all_rewards))
        return np.array(rewards), np.array(optimal_rewards)

    def run_once(self, seed):
        design_matrix = np.ndarray((self.num_time_steps, self.environment.num_predictors))
        rewards = np.ndarray(self.num_time_steps)
        optimal_rewards = np.ndarray(self.num_time_steps)
        arm_selected = np.ndarray(self.num_time_steps, dtype=np.uint)

        self.model.seed(seed).reset()
        self.environment.seed(seed)

        logger.info(f'Experiment_{seed} beginning...')
        for t in range(self.num_time_steps):
            if (t + 1) % self.logging_frequency == 0:
                logger.info(f'Experiment_{seed} at t={t + 1}')

            try:
                arm_selected[t] = self.model.choose_arm(self.environment.arm_contexts)
            except:  # TODO: use models.NotFitted
                arm_selected[t] = self.environment.random_arm()

            design_matrix[t], rewards[t], optimal_rewards[t] = \
                self.environment.choose_arm(arm_selected[t])
            self.model.fit(design_matrix[:t], rewards[:t])

        logger.info(f'Experiment_{seed} complete.')
        return rewards, optimal_rewards
