from copy import deepcopy

import numpy as np

import torch
import torch.optim as optim

from itertools import chain

from mushroom.algorithms.actor_critic import ReparametrizationAC
from mushroom.policy import Policy
from mushroom.approximators import Regressor
from mushroom.approximators.parametric import TorchApproximator
from mushroom.utils.replay_memory import ReplayMemory


class SACPolicy(Policy):
    """
    Class used to implement the policy used by the Soft Actor-Critic
    algorithm. The policy is a Gaussian policy squashed by a tanh.
    This class implements the compute_action_and_log_prob and the
    compute_action_and_log_prob_t methods, that are fundamental for
    the internals calculations of the SAC algorithm.

    """
    def __init__(self, mu_approximator, sigma_approximator, min_a, max_a):
        """
        Constructor.

        Args:
            mu_approximator (Regressor): a regressor computing mean in given a state;
            sigma_approximator (Regressor): a regressor computing the variance in
               given a state;
            min_a (np.ndarray): a vector specifying the minimum action value for
                each component;
            max_a (np.ndarray): a vector specifying the maximum action value for
                each component.

        """
        self._mu_approximator = mu_approximator
        self._sigma_approximator = sigma_approximator
        self._delta_a = torch.from_numpy(0.5*(max_a - min_a))
        self._central_a = torch.from_numpy(0.5*(max_a + min_a))

    def __call__(self, state, action, use_log=True, tensor_output=False):
        raise NotImplementedError

    def draw_action(self, state):
        return self.compute_action_and_log_prob_t(state, compute_log_prob=False).detach().cpu().numpy()

    def compute_action_and_log_prob(self, state):
        a, log_prob = self.compute_action_and_log_prob_t(state)
        log_prob = log_prob.flatten()
        return a.detach().cpu().numpy(), log_prob.detach().cpu().numpy()

    def compute_action_and_log_prob_t(self, state, compute_log_prob=True):
        dist = self.distribution(state)
        a_raw = dist.sample()
        a = torch.tanh(a_raw)
        a_true = a*self._delta_a + self._central_a

        if compute_log_prob:
            log_prob = dist.log_prob(a_raw)
            log_prob -= torch.log(1. - a.pow(2) + 1e-6)
            log_prob = log_prob.sum(dim=1, keepdim=True)
            return a_true, log_prob
        else:
            return a_true

    def distribution(self, state):
        mu = self._mu_approximator.predict(state, output_tensor=True)
        log_sigma = self._sigma_approximator.predict(state, output_tensor=True)
        return torch.distributions.Normal(loc=mu, scale=log_sigma.exp())

    def reset(self):
        pass


class SAC(ReparametrizationAC):
    """
    Soft Actor-Critic algorithm.
    "Soft Actor-Critic Algorithms and Applications".
    Haarnoja T. et al.. 2019
    """
    def __init__(self, mdp_info,
                 batch_size, initial_replay_size, max_replay_size,
                 warmup_transitions, tau, lr_alpha,
                 actor_mu_params, actor_sigma_params,
                 actor_optimizer, critic_params, critic_fit_params=None):
        """
        Constructor.

        Args:
            batch_size (int): the number of samples in a batch;
            initial_replay_size (int): the number of samples to collect before
                starting the learning;
            max_replay_size (int): the maximum number of samples in the replay
                memory;
            warmup_transitions (int): number of samples to accumulate in the
                replay memory to start the policy fitting;
            tau (float): value of coefficient for soft updates;
            lr_alpha (float): Learning rate for the entropy coefficient;
            actor_mu_params (dict): parameters of the actor mean approximator
                to build;
            actor_sigma_params (dict): parameters of the actor sigma approximator
                to build;
            actor_optimizer (dict): parameters to specify the actor
                optimizer algorithm;
            critic_params (dict): parameters of the critic approximator to
                build;
            critic_fit_params (dict, None): parameters of the fitting algorithm
                of the critic approximator;
        """
        self._critic_fit_params = dict() if critic_fit_params is None else critic_fit_params

        self._batch_size = batch_size
        self._warmup_transitions = warmup_transitions
        self._tau = tau
        self._target_entropy = - mdp_info.action_space.shape[0]

        self._replay_memory = ReplayMemory(initial_replay_size, max_replay_size)

        if 'n_models' in critic_params.keys():
            assert critic_params['n_models'] == 2
        else:
            critic_params['n_models'] = 2

        if 'prediction' in critic_params.keys():
            assert critic_params['prediction'] == 'min'
        else:
            critic_params['prediction'] = 'min'

        target_critic_params = deepcopy(critic_params)
        self._critic_approximator = Regressor(TorchApproximator,
                                              **critic_params)
        self._target_critic_approximator = Regressor(TorchApproximator,
                                                     **target_critic_params)

        self._log_alpha = torch.tensor(0., requires_grad=True, dtype=torch.float32)
        self._alpha_optim = optim.Adam([self._log_alpha], lr=lr_alpha)

        actor_mu_approximator = Regressor(TorchApproximator,
                                          **actor_mu_params)
        actor_sigma_approximator = Regressor(TorchApproximator,
                                             **actor_sigma_params)

        policy = SACPolicy(actor_mu_approximator,
                           actor_sigma_approximator,
                           mdp_info.action_space.low,
                           mdp_info.action_space.high)

        self._init_target()

        policy_parameters = chain(actor_mu_approximator.model.network.parameters(),
                                  actor_sigma_approximator.model.network.parameters())
        super().__init__(policy, mdp_info, actor_optimizer, policy_parameters)

    def fit(self, dataset):
        self._replay_memory.add(dataset)
        if self._replay_memory.initialized:
            state, action, reward, next_state, absorbing, _ = \
                self._replay_memory.get(self._batch_size)

            if self._replay_memory.size > self._warmup_transitions:
                action_new, log_prob = self.policy.compute_action_and_log_prob_t(state)
                loss = self._loss(state, action_new, log_prob)
                self._optimize_actor_parameters(loss)
                self._update_alpha(log_prob.detach())

            q_next = self._next_q(next_state, absorbing)
            q = reward + self.mdp_info.gamma * q_next

            self._critic_approximator.fit(state, action, q,
                                          **self._critic_fit_params)

            self._update_target()

    def _init_target(self):
        """
        Init weights for target approximators

        """
        for i in range(len(self._critic_approximator)):
            self._target_critic_approximator.model[i].set_weights(
                self._critic_approximator.model[i].get_weights())

    def _loss(self, state, action_new, log_prob):
        q_0 = self._critic_approximator(state, action_new,
                                        output_tensor=True, idx=0)
        q_1 = self._critic_approximator(state, action_new,
                                        output_tensor=True, idx=1)

        q = torch.min(q_0, q_1)

        return (self._alpha * log_prob - q).mean()

    def _update_alpha(self, log_prob):
        alpha_loss = - (self._log_alpha * (log_prob + self._target_entropy)).mean()
        self._alpha_optim.zero_grad()
        alpha_loss.backward()
        self._alpha_optim.step()

    def _update_target(self):
        """
        Update the target networks.

        """
        for i in range(len(self._target_critic_approximator)):
            critic_weights_i = self._tau * self._critic_approximator.model[i].get_weights()
            critic_weights_i += (1 - self._tau) * self._target_critic_approximator.model[i].get_weights()
            self._target_critic_approximator.model[i].set_weights(critic_weights_i)

    def _next_q(self, next_state, absorbing):
        """
        Args:
            next_state (np.ndarray): the states where next action has to be
                evaluated;
            absorbing (np.ndarray): the absorbing flag for the states in
                ``next_state``.

        Returns:
            Action-values returned by the critic for ``next_state`` and the
            action returned by the actor.

        """
        a, log_prob_next = self.policy.compute_action_and_log_prob(next_state)

        q = self._target_critic_approximator.predict(next_state, a) - self._alpha_np * log_prob_next
        q *= 1 - absorbing

        # print('q', q.shape)
        # print('target', self._target_critic_approximator.predict(next_state, a).shape)
        # print('log_prob', log_prob_next.shape)
        # print('alpha', self._alpha_np)
        # exit()

        return q

    @property
    def _alpha(self):
        return self._log_alpha.exp()

    @property
    def _alpha_np(self):
        return self._alpha.detach().cpu().numpy()
