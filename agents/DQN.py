import numpy as np

import torch
import torch.optim as optim

from utils.hyperparameters import *
from networks.networks import DQN, DQN_simple
from utils.ReplayMemory import ExperienceReplayMemory, PrioritizedReplayMemory

class Model(object):
    def __init__(self, static_policy=False, env=None):
        super(Model, self).__init__()
        self.noisy=USE_NOISY_NETS
        self.priority_replay=USE_PRIORITY_REPLAY

        self.gamma=GAMMA
        self.lr = LR
        self.target_net_update_freq = TARGET_NET_UPDATE_FREQ
        self.experience_replay_size = EXP_REPLAY_SIZE
        self.batch_size = BATCH_SIZE
        self.learn_start = LEARN_START
        self.sigma_init=SIGMA_INIT
        self.priority_beta_start = PRIORITY_BETA_START
        self.priority_beta_frames = PRIORITY_BETA_FRAMES
        self.priority_alpha = PRIORITY_ALPHA

        self.num_actions = env.action_space.n
        self.env = env

        self.static_policy=static_policy

        self.declare_networks()
            
        self.target_model.load_state_dict(self.model.state_dict())
        self.optimizer = optim.Adam(self.model.parameters(), lr=self.lr)
        
        #move to correct device
        self.model = self.model.to(device)
        self.target_model.to(device)

        if self.static_policy:
            self.model.eval()
            self.target_model.eval()
        else:
            self.model.train()
            self.target_model.train()

        self.update_count = 0

        self.memory = ExperienceReplayMemory(self.experience_replay_size) if not self.priority_replay else PrioritizedReplayMemory(self.experience_replay_size, self.priority_alpha, self.priority_beta_start, self.priority_beta_frames)

        self.nsteps = N_STEPS
        self.nstep_buffer = []

    def declare_networks(self):
        self.model = DQN_simple(self.env.observation_space.shape, self.env.action_space.n, noisy=self.noisy, sigma_init=self.sigma_init)
        self.target_model = DQN_simple(self.env.observation_space.shape, self.env.action_space.n, noisy=self.noisy, sigma_init=self.sigma_init)

    def append_to_replay(self, s, a, r, s_):
        self.nstep_buffer.append((s, a, r, s_))

        if(len(self.nstep_buffer)<self.nsteps):
            return
        
        R = sum([self.nstep_buffer[i][2]*(self.gamma**i) for i in range(self.nsteps)])
        state, action, _, _ = self.nstep_buffer.pop(0)

        state = [state]
        action = [[action]]
        reward = [[R]]
        if s_ is None:
            next_state = None
        else:
            next_state = [s_]

        self.memory.push((state, action, reward, next_state))


    def prep_minibatch(self):
        # random transition batch is taken from experience replay memory
        if self.priority_replay:
            transitions, indices, weights = self.memory.sample(BATCH_SIZE)
        else:
            transitions = self.memory.sample(BATCH_SIZE)
            indices, weights = None, None
        
        batch_state, batch_action, batch_reward, batch_next_state = zip(*transitions)

        shape = (-1,)+self.env.observation_space.shape

        batch_state = torch.tensor(batch_state, device=device, dtype=torch.float).view(shape)
        batch_action = torch.tensor(batch_action, device=device, dtype=torch.long).squeeze().view(-1, 1)
        batch_reward = torch.tensor(batch_reward, device=device, dtype=torch.float).squeeze().view(-1, 1)
        
        non_final_mask = torch.tensor(tuple(map(lambda s: s is not None, batch_next_state)), device=device, dtype=torch.uint8)
        try: #sometimes all next states are false
            non_final_next_states = torch.tensor([s for s in batch_next_state if s is not None], device=device, dtype=torch.float).view(shape)
            empty_next_state_values = False
        except:
            non_final_next_states = None
            empty_next_state_values = True

        return batch_state, batch_action, batch_reward, non_final_next_states, non_final_mask, empty_next_state_values, indices, weights

    def compute_loss(self, batch_vars):
        batch_state, batch_action, batch_reward, non_final_next_states, non_final_mask, empty_next_state_values, indices, weights = batch_vars

        #estimate
        self.model.sample_noise()
        current_q_values = self.model(batch_state).gather(1, batch_action)
        
        #target
        with torch.no_grad():
            max_next_q_values = torch.zeros(self.batch_size, device=device, dtype=torch.float).unsqueeze(dim=1)
            if not empty_next_state_values:
                max_next_action = self.get_max_next_state_action(non_final_next_states)
                self.target_model.sample_noise()
                max_next_q_values[non_final_mask] = self.target_model(non_final_next_states).gather(1, max_next_action)
            expected_q_values = batch_reward + ((self.gamma**self.nsteps)*max_next_q_values)

        diff = (expected_q_values - current_q_values)
        loss = self.huber(diff).squeeze()
        if self.priority_replay:
            self.memory.update_priorities(indices, loss.detach().cpu().numpy().tolist())
            loss = loss*weights        
        loss = loss.mean()

        return loss

    def update(self, s, a, r, s_, frame=0):
        if self.static_policy:
            return None

        self.append_to_replay(s, a, r, s_)

        if frame < self.learn_start:
            return None

        batch_vars = self.prep_minibatch()

        loss = self.compute_loss(batch_vars)

        # Optimize the model
        self.optimizer.zero_grad()
        loss.backward()
        '''for param in self.model.parameters():
            param.grad.data.clamp_(-1, 1)'''
        self.optimizer.step()

        self.update_target_model()
        return loss.item()


    def get_action(self, s, eps=0.1):
        with torch.no_grad():
            if np.random.random() >= eps or self.static_policy or self.noisy:
                X = torch.tensor([s], device=device, dtype=torch.float)
                a = self.model(X).max(1)[1].view(1, 1)
                return a.item()
            else:
                return np.random.randint(0, self.num_actions)

    def update_target_model(self):
        self.update_count+=1
        self.update_count = self.update_count % self.target_net_update_freq
        if self.update_count == 0:
            self.target_model.load_state_dict(self.model.state_dict())

    def get_max_next_state_action(self, next_states):
        return self.target_model(next_states).max(dim=1)[1].view(-1, 1)

    def finish_nstep(self):
        while len(self.nstep_buffer) > 0:
            R = sum([self.nstep_buffer[i][2]*(self.gamma**i) for i in range(len(self.nstep_buffer))])
            state, action, _, _ = self.nstep_buffer.pop(0)

            state = [state]
            action = [[action]]
            reward = [[R]]

            self.memory.push((state, action, reward, None))

    def huber(self, x):
        cond = (x < 1.0).float().detach()
        return 0.5 * x.pow(2) * cond + (x.abs() - 0.5) * (1.0 - cond)