# modified from https://github.com/pavlosSkev/ipg_ppo/blob/main/core.py
import numpy as np
import scipy.signal
from gymnasium .spaces import Box, Discrete


import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.distributions.normal import Normal
from torch.distributions.categorical import Categorical
from utils import instantiate_from_config


def combined_shape(length, shape=None):
    if shape is None:
        return (length,)
    return (length, shape) if np.isscalar(shape) else (length, *shape)


def mlp(sizes, activation, output_activation=nn.Identity):
    layers = []
    if isinstance(activation, str):
        activation = getattr(nn, activation)
    for j in range(len(sizes) - 1):
        act = activation if j < len(sizes) - 2 else output_activation
        layers += [nn.Linear(sizes[j], sizes[j + 1]), act()]
    return nn.Sequential(*layers)


def count_vars(module):
    return sum([np.prod(p.shape) for p in module.parameters()])


def discount_cumsum(x, discount):
    """
    magic from rllab for computing discounted cumulative sums of vectors.

    input:
        vector x,
        [x0,
         x1,
         x2]

    output:
        [x0 + discount * x1 + discount^2 * x2,
         x1 + discount * x2,
         x2]
    """
    return scipy.signal.lfilter([1], [1, float(-discount)], x[::-1], axis=0)[::-1]


class Actor(nn.Module):

    def _distribution(self, obs):
        raise NotImplementedError

    def _log_prob_from_distribution(self, pi, act):
        raise NotImplementedError

    def forward(self, obs, act=None): #return action from here
        # Produce action distributions for given observations, and
        # optionally compute the log likelihood of given actions under
        # those distributions.
        pi = self._distribution(obs)
        logp_a = None
        if act is not None:
            logp_a = self._log_prob_from_distribution(pi, act)
        return pi, logp_a


class MLPCategoricalActor(Actor):

    def __init__(self, obs_dim, act_dim, hidden_sizes, activation):
        super().__init__()
        self.logits_net = mlp([obs_dim] + list(hidden_sizes) + [act_dim], activation)

    def _distribution(self, obs):
        logits = self.logits_net(obs)
        logits = F.softmax(logits, dim=-1)
        return Categorical(logits=logits)

    def _log_prob_from_distribution(self, pi, act):
        return pi.log_prob(act)


class MLPGaussianActor(Actor):

    def __init__(self, obs_dim, act_dim, hidden_sizes, activation):
        super().__init__()
        log_std = -0.5 * np.ones(act_dim, dtype=np.float32)
        self.log_std = torch.nn.Parameter(torch.as_tensor(log_std))
        self.mu_net = mlp([obs_dim] + list(hidden_sizes) + [act_dim], activation)

    def _distribution(self, obs):
        mu = self.mu_net(obs)
        std = torch.exp(self.log_std)
        return Normal(mu, std)

    def _get_mean_sigma(self, obs):
        with torch.no_grad():
            mu = self.mu_net(obs)
            std = torch.exp(self.log_std)
            return mu, std

    def _log_prob_from_distribution(self, pi, act):
        return pi.log_prob(act).sum(axis=-1)  # Last axis sum needed for Torch Normal distribution
    

class MLPCritic(nn.Module):

    def __init__(self, obs_dim, hidden_sizes, activation):
        super().__init__()
        self.v_net = mlp([obs_dim] + list(hidden_sizes) + [1], activation)

    def forward(self, obs):
        return torch.squeeze(self.v_net(obs), -1)  # Critical to ensure v has right shape.


# policy and value function
class MLPActorCritic(nn.Module):

    def __init__(self, observation_space, action_space,
                 ac_hidden_sizes=(64, 64), qf_hidden_sizes=(64, 64), vf_hidden_sizse=(64, 64),
                 ac_activation=nn.Tanh, qf_activation=nn.ReLU, vf_activation=nn.Tanh, env_params=None, *args, **kwargs):
        super().__init__()

        if env_params:
            obs_dim = env_params['obs'] + env_params['goal']
        else:
            obs_dim = observation_space.shape[0]

        self.obs_dim = obs_dim

        # policy builder depends on action space
        if isinstance(action_space, Box):
            self.discrete = False
            self.act_dim = action_space.shape[0]
            self.pi = MLPGaussianActor(obs_dim, action_space.shape[0], ac_hidden_sizes, ac_activation)
        elif isinstance(action_space, Discrete):
            self.discrete = True
            self.act_dim = action_space.n
            self.pi = MLPCategoricalActor(obs_dim, action_space.n, ac_hidden_sizes, ac_activation)
        else:
            raise NotImplementedError("Unknown action space")


        # build value function
        self.v = MLPCritic(obs_dim, vf_hidden_sizse, vf_activation)

        if env_params:
            obs_dim_qf = env_params['obs'] + env_params['goal'] + env_params['action']
        else:
            if isinstance(action_space, Box):
                obs_dim_qf = observation_space.shape[0] + action_space.shape[0]
            else:
                obs_dim_qf = observation_space.shape[0] + action_space.n
        # build q function


        self.qf = QFunction(obs_dim_qf, qf_hidden_sizes, qf_activation)
        self.qf_targ = QFunction(obs_dim_qf, qf_hidden_sizes, qf_activation)
        
        # copy params from qf to qf_targ
        for targ_param, param in zip(self.qf_targ.parameters(), self.qf.parameters()):
            targ_param.data.copy_(param.data)
        # freeze qf_targ
        self.qf_targ.freeze()

    @torch.no_grad()
    def step(self, obs):
        if isinstance(self.pi, MLPGaussianActor):
            pi = self.pi._distribution(obs) # distribution with mu and std
            a = pi.sample() # sample action from this distribution
            logp_a = self.pi._log_prob_from_distribution(pi, a)
            v = self.v(obs)
            mean, std = self.pi._get_mean_sigma(obs)
            return a.numpy(), v.numpy(), logp_a.numpy(), mean.numpy(), std.numpy()
        else:
            with torch.no_grad():
                pi = self.pi._distribution(obs)  # categorical distribution
                a = pi.sample() # sample action from this distribution
                logp_a = self.pi._log_prob_from_distribution(pi, a)
                v = self.v(obs)
            return a.numpy(), v.numpy(), logp_a.numpy()


    def act(self, obs):
        return self.step(obs)[0]

    def compute_loss_off_pi(self, data):
        obs = data['obs']
        if isinstance(self.pi, MLPCategoricalActor):
            pi = self.pi._distribution(obs) 
            mu = pi.sample()
            # use action with maximum probability?
            mu = F.one_hot(mu.long(), num_classes=self.act_dim).float()
        else:
            # off policy deterministic policy gradient
            mu = self.pi.mu_net(obs)
        off_loss = self.qf(obs, mu)
        return -(off_loss).mean()
        
    def compute_loss_v(self, data):
        obs, ret = data['obs'], data['ret']
        return F.mse_loss(self.v(obs), ret)
    
    def compute_loss_qf(self, data, gamma):
        obs, act, r, obs_next, dones = data['obs'], data['act'], data['rew'], data['obs2'], data['done']
        if self.discrete:
            act = F.one_hot(act.long(), num_classes=self.act_dim).float()
        q_value_real = self.qf(obs, act)
        with torch.no_grad():
            # TODO: policy target?
            # act_next, _, _, _, _ = ac.step(obs_next) # this samples an action from a distribution
            if isinstance(self.pi, MLPGaussianActor):
                act_next = self.pi.mu_net(obs_next) # this gets the mean of the distribution, corresponding to a deterministic action
            else:
                act_next = self.pi.logits_net(obs_next) 
                # use action with maximum probability?
                act_next = F.one_hot(torch.argmax(act_next, dim=-1), num_classes=self.act_dim).float()

            q_next_value = self.qf_targ(obs_next, act_next)
            q_next_value = q_next_value.detach()  # detach tensor from graph
            # Bellman backup for Q function
            q_value_target = r + gamma * (1 - dones) * q_next_value

        return F.mse_loss(q_value_target, q_value_real)

    def get_expected_q(self, obs):
        if isinstance(self.pi, MLPGaussianActor):
            mu, std = self.pi._get_mean_sigma(obs)
            actions_noise = torch.normal(mean=0, std=1, size=mu.shape) * std + mu  # get_expected_q(mu, std)
            return self.qf(obs, actions_noise)
        else:
            with torch.no_grad():
                logits = self.pi.logits_net(obs)
                actions = Categorical(logits=logits).sample()
            return self.qf(obs, actions)

    def get_control_variate(self, data, cv_type='reparam_critic_cv'):

        obs, act = data['obs'], data['act']
        if cv_type == 'reparam_critic_cv':
            # with torch.no_grad(): #makes it worse. It needs to be in the graph
            q_value_real = self.qf(obs, act)
            q_value_expected = self.get_expected_q(obs)
            return q_value_real - q_value_expected  #from 3.1 of interpolated policy gradients paper            
        else:
            raise ValueError(f"Wrong value for parameter: cv_type, value: {cv_type} does not exist")


# IPG paper: Q function 2 hidden layer 100-100 with ReLU act
class QFunction(nn.Module):
    def __init__(self, obs_dim, hidden_sizes=(100, 100), activation=nn.ReLU, env_params=None):
        # TODO: perform check to create either continous or discrete
        super(QFunction, self).__init__()

        self.q_func = mlp([obs_dim] + list(hidden_sizes) + [1], activation)

    def forward(self, obs, act):
        input_tensor = torch.cat([obs, act], dim=1)
        q_value = self.q_func(input_tensor)
        return torch.squeeze(q_value, -1) #q_value
    
    def freeze(self):
        for param in self.parameters():
            param.requires_grad = False

    def unfreeze(self):
        for param in self.parameters():
            param.requires_grad = True

