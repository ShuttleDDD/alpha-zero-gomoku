from random import shuffle, sample
import numpy as np
from collections import deque
import threading
import time
import math

from neural_network import NeuralNetWorkWrapper
from gomoku_gui import GomokuGUI
from swig import MCTS, ThreadPool, Gomoku, VirtualNeuralNetwork


def tuple_2d_to_numpy_2d(tuple_2d):
    # help function
    # convert type
    res = [None] * len(tuple_2d)
    for i, tuple_1d in enumerate(tuple_2d):
        res[i] = list(tuple_1d)
    return np.array(res)

class CallbackNeuralNetwork(VirtualNeuralNetwork):
    # for swig callback inferface
    # Define Python class 'constructor'
    def __init__(self, neural_network):
        VirtualNeuralNetwork.__init__(self)
        self.neural_network = neural_network

    # Override C++ method
    def infer(self, gomoku):
        board = tuple_2d_to_numpy_2d(gomoku.get_board())
        last_move = gomoku.get_last_move()
        cur_player_color = gomoku.get_current_color()

        p_batch, v_batch = self.neural_network.infer([(board, last_move, cur_player_color)])
        return [p_batch[0].tolist(), v_batch[0].tolist()]


class Leaner():
    def __init__(self, config):
        # config see README.md
        # gomoku
        self.n = config['n']
        self.n_in_row = config['n_in_row']
        self.gomoku_gui = GomokuGUI(config['n'])
        self.action_size = self.n ** 2

        # train
        self.num_iters = config['num_iters']
        self.num_eps = config['num_eps']
        self.check_freq = config['check_freq']
        self.contest_num = config['contest_num']
        self.dirichlet_alpha = config['dirichlet_alpha']
        self.temp = config['temp']
        self.update_threshold = config['update_threshold']
        self.explore_num = config['explore_num']

        self.examples_buffer = deque([], maxlen=config['examples_buffer_max_len'])

        # neural network
        self.batch_size = config['batch_size']

        self.nnet = NeuralNetWorkWrapper(config['lr'], config['l2'], config['kl_targ'], config['epochs'],
                                         config['num_channels'], config['n'], self.action_size)
        self.nnet_best = NeuralNetWorkWrapper(config['lr'], config['l2'], config['kl_targ'],
                                              config['epochs'], config['num_channels'], config['n'], self.action_size)

        # add callbacks for MCTS
        self.nnet_cb = CallbackNeuralNetwork(self.nnet)
        self.nnet_best_cb = CallbackNeuralNetwork(self.nnet_best)

        # mcts
        self.num_mcts_sims = config['num_mcts_sims']
        self.c_puct = config['c_puct']
        self.c_virtual_loss = config['c_virtual_loss']

        self.thread_pool = ThreadPool(config['thread_pool_size'])

    def learn(self):
        # train the model by self play

        self.nnet.save_model(filename="best_checkpoint")

        for i in range(1, self.num_iters + 1):
            print("ITER ::: " + str(i))

            # self play
            first_color = 1
            for eps in range(1, self.num_eps + 1):
                self.examples_buffer.extend(self.self_play(first_color))
                first_color = -first_color
                print("EPS :: " + str(eps) + ", EXAMPLES :: " + str(len(self.examples_buffer)))

            # sample train data
            if len(self.examples_buffer) < self.batch_size:
                continue

            print("sampling...")
            train_data = sample(self.examples_buffer, self.batch_size)

            # train neural network
            self.nnet.save_model(filename="checkpoint")
            self.nnet.train(train_data)

            if i % self.check_freq == 0:
                # compare performance
                self.nnet_best.load_model(filename="best_checkpoint")

                mcts = MCTS(self.thread_pool, self.nnet_cb, self.c_puct,
                            self.num_mcts_sims, self.c_virtual_loss, self.action_size)
                mcts_best = MCTS(self.thread_pool, self.nnet_best_cb, self.c_puct,
                                 self.num_mcts_sims, self.c_virtual_loss, self.action_size)

                one_won, two_won, draws = self.contest(mcts, mcts_best, self.contest_num)
                print("NEW/PREV WINS : %d / %d ; DRAWS : %d" % (one_won, two_won, draws))

                if one_won + two_won > 0 and float(one_won) / (one_won + two_won) > self.update_threshold:
                    print('REJECTING NEW MODEL')
                else:
                    print('ACCEPTING NEW MODEL')
                    self.nnet.save_model(filename="best_checkpoint")

    def self_play(self, first_color):
        """
        This function executes one episode of self-play, starting with player 1.
        As the game is played, each turn is added as a training example to
        train_examples. The game is played till the game ends. After the game
        ends, the outcome of the game is used to assign values to each example
        in train_examples.
        """

        train_examples = []
        gomoku = Gomoku(self.n, self.n_in_row, first_color)
        mcts = MCTS(self.thread_pool, self.nnet_cb, self.c_puct,
                    self.num_mcts_sims, self.c_virtual_loss, self.action_size)

        episode_step = 0
        while True:
            episode_step += 1

            # temperature
            temp = self.temp if episode_step <= self.explore_num else 0

            prob =  np.array(list(mcts.get_action_probs(gomoku, temp)))

            board = tuple_2d_to_numpy_2d(gomoku.get_board())
            last_action = gomoku.get_last_move()
            cur_player = gomoku.get_current_color()

            sym = self.get_symmetries(board, prob)
            for b, p in sym:
                train_examples.append([b, last_action, cur_player, p])

            # Dirichlet noise
            valid_action_num = 0
            for p in prob:
                if not math.fabs(p - 0) < 1e-6:
                    valid_action_num += 1
            noise = 0.25 * np.random.dirichlet(self.dirichlet_alpha * np.ones(valid_action_num))

            prob_noise = 0.75 * prob
            j = 0
            for i, p in enumerate(prob_noise):
                if not math.fabs(p - 0) < 1e-6:
                    prob_noise[i] += noise[j]
                    j += 1
            prob_noise /= np.sum(prob_noise)
            action = np.random.choice(len(prob_noise), p=prob_noise)

            gomoku.execute_move(action)
            mcts.update_with_move(action)
            ended, winner = gomoku.get_game_status()

            # GAME OVER
            if ended == 1:
                # b, last_action, cur_player, p, v
                return [(x[0], x[1], x[2], x[3], x[2] * winner) for x in train_examples]

    def contest(self, player1, player2, contest_num):
        """compare new and old model
           Args: player1, player2 is MCTS object
           Return: one_won, two_won, draws
        """

        one_won, two_won, draws = 0, 0, 0
        contest_num //= 2

        # first half, white first
        for _ in range(contest_num):
            players = [player2, None, player1]
            player_index = 1
            gomoku = Gomoku(self.n, self.n_in_row, player_index)

            ended, winner = gomoku.get_game_status()
            while ended == 0:
                player =  players[player_index + 1]
                player_index = -player_index

                prob = player.get_action_prob(gomoku)
                best_move = int(np.argmax(np.array(list(prob))))
                gomoku.execute_move(best_move)

                # update search tree
                player1.update_with_move(best_move)
                player2.update_with_move(best_move)

            if winner == 1:
                one_won += 1
            elif winner == -1:
                two_won += 1
            else:
                draws += 1

        # second half, black first
        for i in range(contest_num):
            players = [player2, None, player1]
            player_index = -1
            gomoku = Gomoku(self.n, self.n_in_row, player_index)

            ended, winner = gomoku.get_game_status()
            while ended == 0:
                player =  players[player_index + 1]
                player_index = -player_index

                prob = player.get_action_prob(gomoku)
                best_move = int(np.argmax(np.array(list(prob))))
                gomoku.execute_move(best_move)

                # update search tree
                player1.update_with_move(best_move)
                player2.update_with_move(best_move)

            if winner == 1:
                one_won += 1
            elif winner == -1:
                two_won += 1
            else:
                draws += 1

        return one_won, two_won, draws

    def get_symmetries(self, board, pi):
        # mirror, rotational
        assert(len(pi) == self.action_size)  # 1 for pass

        pi_board = np.reshape(pi, (self.n, self.n))
        l = []

        for i in range(1, 5):
            for j in [True, False]:
                newB = np.rot90(board, i)
                newPi = np.rot90(pi_board, i)
                if j:
                    newB = np.fliplr(newB)
                    newPi = np.fliplr(newPi)
                l += [(newB, newPi.ravel())]
        return l