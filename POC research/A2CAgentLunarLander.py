#%% Imports & Setup
import gymnasium as gym
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
import matplotlib.pyplot as plt
from gymnasium.vector import AsyncVectorEnv
import multiprocessing

#%% Hyperparameters
num_envs = 8
num_episodes = 2500
max_steps = 500
gamma = 0.99
lr = 1e-3
render_every = 50  # Visualize every N episodes

#%% Actor-Critic Network
class ActorCritic(nn.Module):
    def __init__(self, state_dim, action_dim):
        super(ActorCritic, self).__init__()
        self.shared = nn.Sequential(
            nn.Linear(state_dim, 128),
            nn.ReLU()
        )
        self.policy = nn.Sequential(
            nn.Linear(128, action_dim),
            nn.Softmax(dim=-1)
        )
        self.value = nn.Linear(128, 1)

    def forward(self, x):
        x = self.shared(x)
        return self.policy(x), self.value(x)

#%% A2C Agent
class A2CAgent:
    def __init__(self, state_dim, action_dim, gamma=0.99, lr=1e-3):
        self.gamma = gamma
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.model = ActorCritic(state_dim, action_dim).to(self.device)
        self.optimizer = optim.Adam(self.model.parameters(), lr=lr)

    def select_action(self, states):
        states = torch.from_numpy(np.array(states)).float().to(self.device)
        probs, values = self.model(states)
        dist = torch.distributions.Categorical(probs)
        actions = dist.sample()
        log_probs = dist.log_prob(actions)
        return actions.cpu().numpy(), log_probs, values.squeeze(-1)

    def update(self, trajectories, entropy_coef=0.01):
        states, log_probs, rewards, dones, values, next_values = trajectories

        states = torch.from_numpy(np.array(states)).float().to(self.device)
        log_probs = torch.stack(log_probs).to(self.device)
        rewards = torch.FloatTensor(rewards).to(self.device)
        dones = torch.FloatTensor(dones).to(self.device)
        values = torch.stack(values).to(self.device)
        next_values = torch.stack(next_values).to(self.device)

        returns = torch.zeros_like(rewards).to(self.device)
        R = next_values[-1]
        for t in reversed(range(len(rewards))):
            R = rewards[t] + self.gamma * R * (1 - dones[t])
            returns[t] = R

        advantages = returns - values
        advantages = (advantages - advantages.mean()) / (advantages.std() + 1e-8)

        # Compute entropy bonus
        with torch.no_grad():
            probs, _ = self.model(states)
        dist = torch.distributions.Categorical(probs)
        entropy = dist.entropy().mean()
        # entropy_coef is now passed in from training loop

        actor_loss = -(log_probs * advantages.detach()).mean()
        critic_loss = advantages.pow(2).mean()
        loss = actor_loss + 0.5 * critic_loss - entropy_coef * entropy

        self.optimizer.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(self.model.parameters(), max_norm=0.5)
        self.optimizer.step()

#%% Environment Creation
def make_env(render_mode=None):
    def thunk():
        return gym.make("LunarLander-v3", render_mode=render_mode)
    return thunk

#%% Main Entry Point
if __name__ == "__main__":
    multiprocessing.freeze_support()

    env = AsyncVectorEnv([make_env(None) for _ in range(num_envs)])
    state_dim = env.single_observation_space.shape[0]
    action_dim = env.single_action_space.n

    agent = A2CAgent(state_dim, action_dim, gamma=gamma, lr=lr)

    #%% Training Loop
    reward_history = []
    state, _ = env.reset()

    best_mean_reward = -float('inf')
    stagnation_counter = 0
    entropy_boost_active = False
    entropy_coef = 0.01

    for episode in range(num_episodes):
        all_rewards = np.zeros(num_envs)
        trajectories = {
            "states": [],
            "log_probs": [],
            "rewards": [],
            "dones": [],
            "values": [],
            "next_values": []
        }

        for t in range(max_steps):
            actions, log_probs, values = agent.select_action(state)
            next_state, reward, terminated, truncated, _ = env.step(actions)
            done = np.logical_or(terminated, truncated)

            with torch.no_grad():
                _, next_values = agent.model(torch.FloatTensor(next_state).to(agent.device))

            trajectories["states"].append(state)
            trajectories["log_probs"].append(log_probs)
            trajectories["rewards"].append(reward)
            trajectories["dones"].append(done.astype(float))
            trajectories["values"].append(values)
            trajectories["next_values"].append(next_values.squeeze(-1))

            all_rewards += reward
            state = next_state

            if np.all(done):
                break

        # Stack valid items for update
        stacked_states = np.stack(trajectories["states"])
        stacked_rewards = np.stack(trajectories["rewards"])
        stacked_dones = np.stack(trajectories["dones"])
        stacked_log_probs = trajectories["log_probs"]
        stacked_values = trajectories["values"]
        stacked_next_values = trajectories["next_values"]

        stacked_trajectories = (
            stacked_states,
            stacked_log_probs,
            stacked_rewards,
            stacked_dones,
            stacked_values,
            stacked_next_values
        )

        agent.update(stacked_trajectories, entropy_coef=entropy_coef)

        mean_reward = all_rewards.mean()
        reward_history.append(mean_reward)
        # Update entropy if no improvement
        if mean_reward > best_mean_reward + 5:
            best_mean_reward = mean_reward
            stagnation_counter = 0
            entropy_coef = 0.01
            entropy_boost_active = False
        else:
            stagnation_counter += 1

        if stagnation_counter >= 100 and not entropy_boost_active:
            print(f"Entropy boost triggered at episode {episode + 1}")
            entropy_coef = 0.05
            entropy_boost_active = True

        print(f"Episode {episode + 1}, Mean Reward: {mean_reward:.2f}, Max: {best_mean_reward:.2f} ~ {stagnation_counter}")

        # Optional rendering of env 0
        if (episode + 1) % render_every == 0:
            video_env = gym.make("LunarLander-v3", render_mode="rgb_array_list")
            obs, _ = video_env.reset()
            frames = []
            for _ in range(max_steps):
                eval_action, _, _ = agent.select_action([obs])
                obs, _, terminated, truncated, _ = video_env.step(eval_action[0])
                if terminated or truncated:
                    break
            video = video_env.render()
            video_env.close()

            import os
            import imageio
            os.makedirs("videos", exist_ok=True)
            output_path = f"videos/episode_{episode + 1}.mp4"
            imageio.mimsave(output_path, video, fps=30)
            print(f"Saved episode {episode + 1} to {output_path}")

    env.close()

    #%% Plotting Results
    def moving_average(data, window_size=20):
        return np.convolve(data, np.ones(window_size)/window_size, mode='valid')

    smoothed = moving_average(reward_history)

    plt.figure(figsize=(10, 5))
    plt.plot(reward_history, alpha=0.3, label="Episode Reward (mean of 8 envs)")
    plt.plot(range(len(smoothed)), smoothed, label="Smoothed (20 episodes)")
    plt.axhline(200, color='red', linestyle='--', label='Solved Threshold (200)')
    plt.xlabel("Episode")
    plt.ylabel("Reward")
    plt.title("Vectorized A2C on LunarLander-v3 (Async + Optional Render)")
    plt.legend()
    plt.grid(True)
    plt.tight_layout()
    plt.show()
