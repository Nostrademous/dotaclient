import logging
from pprint import pformat, pprint
from collections import namedtuple

import numpy as np
import torch
import torch.nn as nn
from torch.distributions import Categorical
import torch.nn.functional as F

logging.basicConfig(format='%(asctime)s %(levelname)-8s %(message)s')
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


TICKS_PER_OBSERVATION = 15 # HACK!
# N_DELAY_ENUMS = 5  # HACK!

Action = namedtuple('Action', ['sample', 'probs', 'log_prob'])

class Policy(nn.Module):

    TICKS_PER_SECOND = 30
    MAX_MOVE_SPEED = 550
    MAX_MOVE_IN_OBS = (MAX_MOVE_SPEED / TICKS_PER_SECOND) * TICKS_PER_OBSERVATION
    N_MOVE_ENUMS = 9
    MOVE_ENUMS = np.arange(N_MOVE_ENUMS, dtype=np.float32) - int(N_MOVE_ENUMS / 2)
    MOVE_ENUMS *= MAX_MOVE_IN_OBS / (N_MOVE_ENUMS - 1) * 2
    MAX_UNITS = 16

    MAX_LEVEL_ABILITY_SELECTIONS = 15

    def __init__(self):
        super().__init__()

        self.affine_env = nn.Linear(3, 128)

        self.affine_player_ability_selection = nn.Linear(self.MAX_LEVEL_ABILITY_SELECTIONS, 128)
        self.affine_unit_basic_stats = nn.Linear(8, 128)

        self.affine_unit_ah = nn.Linear(128, 128)
        self.affine_unit_eh = nn.Linear(128, 128)
        self.affine_unit_anh = nn.Linear(128, 128)
        self.affine_unit_enh = nn.Linear(128, 128)

        self.affine_pre_rnn = nn.Linear(640, 128)
        self.rnn = nn.LSTM(input_size=128, hidden_size=128, num_layers=1)

        # self.ln = nn.LayerNorm(128)

        # Heads
        self.affine_head_enum = nn.Linear(128, 3)
        self.affine_move_x = nn.Linear(128, self.N_MOVE_ENUMS)
        self.affine_move_y = nn.Linear(128, self.N_MOVE_ENUMS)
        # self.affine_head_delay = nn.Linear(128, N_DELAY_ENUMS)
        self.affine_unit_attention = nn.Linear(128, 128)

    def single(self, hidden, **kwargs):
        """Inputs a single element of a sequence."""
        for k in kwargs:
            kwargs[k] = kwargs[k].unsqueeze(0)
        return self.__call__(**kwargs, hidden=hidden)

    def forward(self, env, ability_leveling, allied_heroes, enemy_heroes,
                allied_nonheroes, enemy_nonheroes, hidden):
        logger.debug('policy(inputs=\n{}'.format(
            pformat({'env': env,
            'ability_leveling': ability_leveling,
            'allied_heroes': allied_heroes,
            'enemy_heroes': enemy_heroes,
            'allied_nonheroes': allied_nonheroes,
            'enemy_nonheroes': enemy_nonheroes,
            })))

        # Environment.
        env = F.relu(self.affine_env(env))  # (128,)

        # Ability Leveling
        ab_lvl_basic = F.relu(self.affine_player_ability_selection(ability_leveling)) # (128,)

        # Allied Heroes.
        ah_basic = F.relu(self.affine_unit_basic_stats(allied_heroes))
        ah_embedding = self.affine_unit_ah(ah_basic)
        ah_embedding_max, _ = torch.max(ah_embedding, dim=1) # (128,)

        # Enemy Heroes.
        eh_basic = F.relu(self.affine_unit_basic_stats(enemy_heroes))
        eh_embedding = self.affine_unit_eh(eh_basic)
        eh_embedding_max, _ = torch.max(eh_embedding, dim=1) # (128,)

        # Allied Non-Heroes.
        anh_basic = F.relu(self.affine_unit_basic_stats(allied_nonheroes))
        anh_embedding = self.affine_unit_anh(anh_basic)
        anh_embedding_max, _ = torch.max(anh_embedding, dim=1) # (128,)

        # Enemy Non-Heroes.
        enh_basic = F.relu(self.affine_unit_basic_stats(enemy_nonheroes))
        enh_embedding = self.affine_unit_enh(enh_basic)
        enh_embedding_max, _ = torch.max(enh_embedding, dim=1) # (128,)

        # Create the full unit embedding
        unit_embedding = torch.cat((ah_embedding, eh_embedding, anh_embedding, enh_embedding), dim=1)  # (n, 128)

        # Combine for LSTM.
        x = torch.cat((env, ah_embedding_max, eh_embedding_max, anh_embedding_max, enh_embedding_max), dim=1)  # (640,)

        x = F.relu(self.affine_pre_rnn(x))  # (640,)

        # TODO(tzaman) Maybe add parameter noise here.
        # x = self.ln(x)

        # LSTM
        x = x.unsqueeze(1)  # Add in fake batch dimension
        x, hidden = self.rnn(x, hidden)  # Works in (seq_len, batch, inputs)

        # Heads.
        action_scores_x = self.affine_move_x(x)
        action_scores_y = self.affine_move_y(x)
        action_scores_enum = self.affine_head_enum(x)
        # action_delay_enum = self.affine_head_delay(x)
        action_unit_attention = self.affine_unit_attention(x)  # shape: (1, 256)

        unit_embedding = torch.transpose(unit_embedding, dim0=2, dim1=1) # (b, units, n) -> (b, n, units)

        action_unit_attention= torch.matmul(action_unit_attention, unit_embedding)   # (b, 1, n) * (b, n, U) = (b, 1, U)

        action_dict = dict(
            enum=F.softmax(action_scores_enum, dim=2),
            x=F.softmax(action_scores_x, dim=2),
            y=F.softmax(action_scores_y, dim=2),
            # delay=F.softmax(action_delay_enum, dim=2),
            target_unit=F.softmax(action_unit_attention, dim=2),
        )

        # TODO(tzaman): what is the correct way to handle invalid actions like below?
        # if action_dict['target_unit'].shape[1] == 0:
        #     # If there are no units to target, we cannot perform 'action'
        #     # TODO(tzaman): come up with something nice and generic here.
        #     x = action_dict['enum'].clone()
        #     x[0][2] = 0  # Mask out 'attack_target'
        #     action_dict['enum'] = x

        return action_dict, hidden


    @staticmethod
    def sample_action(probs):
        return Categorical(probs).sample()

    @staticmethod
    def action_log_prob(probs, sample):
        return Categorical(probs).log_prob(sample)

    @classmethod
    def select_actions(cls, head_prob_dict):
        # From all heads, select actions.
        action_dict = {}
        # First select the high-level action.
        action_dict['enum'] = cls.sample_action(head_prob_dict['enum'])

        if action_dict['enum'] == 0:  # Nothing
            pass
        elif action_dict['enum'] == 1:  # Move
            action_dict['x'] = cls.sample_action(head_prob_dict['x'])
            action_dict['y'] = cls.sample_action(head_prob_dict['y'])
        elif action_dict['enum'] == 2:  # Attack
            if head_prob_dict['target_unit'].size(1) != 0:
                action_dict['target_unit'] = cls.sample_action(head_prob_dict['target_unit'])
        else:
            ValueError("Invalid Action Selection.")

        return action_dict

    @classmethod
    def action_probs(cls, head_prob_dict, action_dict):
        # Given heads (probabilities) and actions distinctly selected from those, join these
        # pieces of information to yield: (1) the action (2) the prob (3) the logprob
        action_probs = {}
        for k, v in action_dict.items():
            action_probs[k] = Action(
                sample=v,
                probs=head_prob_dict[k],
                log_prob=cls.action_log_prob(probs=head_prob_dict[k], sample=v),
                )
        return action_probs


class RndModel(torch.nn.Module):

    def __init__(self, requires_grad):
        super().__init__()
        self.affine1 = torch.nn.Linear(10, 64)
        self.affine2 = torch.nn.Linear(64, 64)
        self.affine3 = torch.nn.Linear(64, 64)
        self.affine4 = torch.nn.Linear(64, 64)
        self.requires_grad = requires_grad

    def forward(self, env, allied_heroes, enemy_heroes, allied_nonheroes, enemy_nonheroes):
        if allied_heroes.size(0) == 0:  # HACK: Dead hero.
            allied_heroes = torch.zeros(1, 8)
        inputs = torch.cat([env.view(-1), allied_heroes.view(-1)])
        x = F.relu(self.affine1(inputs))
        x = F.relu(self.affine2(x))
        x = F.relu(self.affine3(x))
        x = F.relu(self.affine4(x))
        return x
