# coding=utf-8
"""
Based on the paper:

http://arxiv.org/pdf/1601.06581v2.pdf

This model is:

acoustic RNN trained with ctc loss
"""

import tensorflow as tf
try:
    from tensorflow.models.rnn import rnn_cell, rnn
except ImportError:
    # TensorFlow >= 0.8
    from tensorflow.python.ops import rnn_cell, rnn
try:
    import tensorflow.contrib.ctc as ctc
except ImportError:
    # TensorFlow >= 0.10
    from tensorflow import nn as ctc
import util.audioprocessor as audioprocessor
import numpy as np
from multiprocessing import Process, Pipe
import time
import sys
import os
from datetime import datetime
from random import shuffle


class AcousticModel(object):
    def __init__(self, session, num_labels, num_layers, hidden_size, dropout,
                 batch_size, learning_rate, lr_decay_factor, grad_clip,
                 max_input_seq_length, max_target_seq_length, input_dim,
                 forward_only=False, tensorboard_dir=None, tb_run_name=None):
        """
        Acoustic rnn model, using ctc loss with lstm cells
        Inputs:
        session - tensorflow session
        num_labels - dimension of character input/one hot encoding
        num_layers - number of lstm layers
        hidden_size - size of hidden layers
        dropout - probability of dropping hidden weights
        batch_size - number of training examples fed at once
        learning_rate - learning rate parameter fed to optimizer
        lr_decay_factor - decay factor of the learning rate
        grad_clip - max gradient size (prevent exploding gradients)
        max_input_seq_length - maximum length of input vector sequence
        max_target_seq_length - maximum length of ouput vector sequence
        input_dim - dimension of input vector
        forward_only - whether to build back prop nodes or not
        tensorboard_dir - path to tensorboard file (None if not activated)
        """
        # Define GraphKeys for TensorBoard
        graphkey_training = tf.GraphKeys()
        graphkey_test = tf.GraphKeys()

        self.dropout = dropout
        self.batch_size = batch_size
        self.learning_rate = tf.Variable(float(learning_rate), trainable=False, name='learning_rate')
        tf.scalar_summary('Learning rate', self.learning_rate, collections=[graphkey_training, graphkey_test])
        self.learning_rate_decay_op = self.learning_rate.assign(self.learning_rate * lr_decay_factor)
        self.global_step = tf.Variable(0, trainable=False, name='global_step')
        self.dropout_keep_prob_lstm_input = tf.constant(self.dropout)
        self.dropout_keep_prob_lstm_output = tf.constant(self.dropout)
        self.max_input_seq_length = max_input_seq_length
        self.max_target_seq_length = max_target_seq_length
        self.tensorboard_dir = tensorboard_dir

        # Initialize data pipes and audio_processor to None
        self.train_conn = None
        self.test_conn = None
        self.audio_processor = None

        # graph inputs
        self.inputs = tf.placeholder(tf.float32,
                                     shape=[self.max_input_seq_length, None, input_dim],
                                     name="inputs")
        # We could take an int16 for less memory consumption but CTC need an int32
        self.input_seq_lengths = tf.placeholder(tf.int32,
                                                shape=[None],
                                                name="input_seq_lengths")
        # Take an int16 for less memory consumption
        # max_target_seq_length should be less than 65535 (which is huge)
        self.target_seq_lengths = tf.placeholder(tf.int16,
                                                 shape=[None],
                                                 name="target_seq_lengths")

        # Define cells of acoustic model
        cell = rnn_cell.BasicLSTMCell(hidden_size, state_is_tuple=True)
        if not forward_only:
            # If we are in training then add a dropoutWrapper to the cells
            cell = rnn_cell.DropoutWrapper(cell, input_keep_prob=self.dropout_keep_prob_lstm_input,
                                           output_keep_prob=self.dropout_keep_prob_lstm_output)

        if num_layers > 1:
            cell = rnn_cell.MultiRNNCell([cell] * num_layers, state_is_tuple=True)

        # build input layer
        with tf.name_scope('Input_Layer'):
            w_i = tf.Variable(tf.truncated_normal([input_dim, hidden_size], stddev=np.sqrt(2.0 / (2 * hidden_size))),
                              name="input_w")
            b_i = tf.Variable(tf.zeros([hidden_size]), name="input_b")

        # make rnn inputs
        inputs = [tf.matmul(tf.squeeze(i, squeeze_dims=[0]), w_i) + b_i
                  for i in tf.split(0, self.max_input_seq_length, self.inputs)]

        # set rnn init state to 0s
        init_state = cell.zero_state(self.batch_size, tf.float32)

        # build rnn
        with tf.name_scope('Dynamic_rnn'):
            rnn_output, self.hidden_state = rnn.dynamic_rnn(cell, tf.pack(inputs),
                                                            sequence_length=self.input_seq_lengths,
                                                            initial_state=init_state,
                                                            time_major=True, parallel_iterations=1000)

        # build output layer
        with tf.name_scope('Output_layer'):
            w_o = tf.Variable(tf.truncated_normal([hidden_size, num_labels], stddev=np.sqrt(2.0 / (2 * num_labels))),
                              name="output_w")
            b_o = tf.Variable(tf.zeros([num_labels]), name="output_b")

        # compute logits
        self.logits = tf.pack([tf.matmul(tf.squeeze(i, squeeze_dims=[0]), w_o) + b_o
                               for i in tf.split(0, self.max_input_seq_length, rnn_output)])

        # compute prediction
        self.prediction = tf.to_int32(ctc.ctc_beam_search_decoder(self.logits, self.input_seq_lengths)[0][0])

        if not forward_only:
            # graph sparse tensor inputs
            # We could take an int16 for less memory consumption but SparseTensor need an int64
            self.target_indices = tf.placeholder(tf.int64,
                                                 shape=[None, 2],
                                                 name="target_indices")
            # We could take an int8 for less memory consumption but CTC need an int32
            self.target_vals = tf.placeholder(tf.int32,
                                              shape=[None],
                                              name="target_vals")

            # setup sparse tensor for input into ctc loss
            sparse_labels = tf.SparseTensor(
                indices=self.target_indices,
                values=self.target_vals,
                shape=[self.batch_size, self.max_target_seq_length])

            # compute ctc loss
            self.ctc_loss = ctc.ctc_loss(self.logits, sparse_labels,
                                         self.input_seq_lengths)
            self.mean_loss = tf.reduce_mean(self.ctc_loss)
            tf.scalar_summary('Mean loss (Training)', self.mean_loss, collections=[graphkey_training])
            tf.scalar_summary('Mean loss (Test)', self.mean_loss, collections=[graphkey_test])
            params = tf.trainable_variables()

            opt = tf.train.AdamOptimizer(self.learning_rate)
            gradients = tf.gradients(self.ctc_loss, params)
            clipped_gradients, norm = tf.clip_by_global_norm(gradients,
                                                             grad_clip)
            self.update = opt.apply_gradients(zip(clipped_gradients, params),
                                              global_step=self.global_step)

            # Accuracy
            with tf.name_scope('Accuracy'):
                errorRate = tf.reduce_sum(tf.edit_distance(self.prediction, sparse_labels, normalize=False)) / \
                           tf.to_float(tf.size(sparse_labels.values))
                tf.scalar_summary('Error Rate (Training)', errorRate, collections=[graphkey_training])
                tf.scalar_summary('Error Rate (Test)', errorRate, collections=[graphkey_test])

        # TensorBoard init
        if self.tensorboard_dir is not None:
            self.train_summaries = tf.merge_all_summaries(key=graphkey_training)
            self.test_summaries = tf.merge_all_summaries(key=graphkey_test)
            if tb_run_name is None:
                run_name = datetime.now().strftime('%Y-%m-%d--%H-%M-%S')
            else:
                run_name = tb_run_name
            self.summary_writer = tf.train.SummaryWriter(tensorboard_dir + '/' + run_name + '/', graph=session.graph)
        else:
            self.summary_writer = None

        # We need to save all variables except for the hidden_state
        # we keep it across batches but we don't need it across different runs
        # Especially when we process a one time file
        save_list = [var for var in tf.all_variables() if var.name.find('hidden_state') == -1]
        self.saver = tf.train.Saver(save_list)

    def getBatch(self, dataset, batch_pointer, is_train):
        """
        Inputs:
          dataset - tuples of (wav file, transcribed_text)
          batch_pointer - start point in dataset from where to take the batch
          is_train - training mode (to choose which pipe to use)
        Returns:
          input_feat_vecs, input_feat_vec_lengths, target_lengths,
            target_labels, target_indices
        """
        input_feat_vecs = []
        input_feat_vec_lengths = []
        target_lengths = []
        target_labels = []
        target_indices = []

        batch_counter = 0
        while batch_counter < self.batch_size:
            file_text = dataset[batch_pointer]
            batch_pointer += 1
            if batch_pointer == dataset.__len__():
                batch_pointer = 0

            # Process the audio file to get the input
            feat_vec, original_feat_vec_length = self.audio_processor.processFLACAudio(file_text[0])
            # Process the label to get the output
            # Labels len does not need to be always the same as for input, don't need padding
            try:
                labels = self.getStrLabels(file_text[1])
            except:
                # Incorrect label
                print("Incorrect label for {0} ({1})".format(file_text[0], file_text[1]))
                continue

            # Check sizes
            if (len(labels) > self.max_target_seq_length) or (original_feat_vec_length > self.max_input_seq_length):
                # If either input or output vector is too long we shouldn't take this sample
                print("Warning - sample too long : {0} (input : {1} / text : {2})".format(file_text[0],
                      original_feat_vec_length, len(labels)))
                continue

            assert len(labels) <= self.max_target_seq_length
            assert len(feat_vec) <= self.max_input_seq_length

            # Add input to inputs matrix and unpadded or cut size to dedicated vector
            input_feat_vecs.append(feat_vec)
            input_feat_vec_lengths.append(min(original_feat_vec_length, self.max_input_seq_length))

            # Compute sparse tensor for labels
            indices = [[batch_counter, i] for i in range(len(labels))]
            target_indices += indices
            target_labels += labels
            target_lengths.append(len(labels))
            batch_counter += 1

        input_feat_vecs = np.swapaxes(input_feat_vecs, 0, 1)
        if is_train and self.train_conn is not None:
            self.train_conn.send([input_feat_vecs, input_feat_vec_lengths,
                                  target_lengths, target_labels, target_indices, batch_pointer])
        elif not is_train and self.test_conn is not None:
            self.test_conn.send([input_feat_vecs, input_feat_vec_lengths,
                                 target_lengths, target_labels, target_indices, batch_pointer])
        else:
            return [input_feat_vecs, input_feat_vec_lengths,
                    target_lengths, target_labels, target_indices, batch_pointer]

    def initializeAudioProcessor(self, max_input_seq_length):
        self.audio_processor = audioprocessor.AudioProcessor(max_input_seq_length)

    def setConnections(self):
        # setting up piplines to be able to load data async (one for test set, one for train)
        parent_train_conn, self.train_conn = Pipe()
        parent_test_conn, self.test_conn = Pipe()
        return parent_train_conn, parent_test_conn

    @staticmethod
    def getStrLabels(_str):
        allowed_chars = "abcdefghijklmnopqrstuvwxyz .'-_"
        # Remove punctuation
        _str = _str.replace(".", "")
        _str = _str.replace(",", "")
        _str = _str.replace("?", "")
        _str = _str.replace("'", "")
        _str = _str.replace("!", "")
        _str = _str.replace(":", "")
        # add eos char
        _str += "_"
        return [allowed_chars.index(char) for char in _str]

    def getNumBatches(self, dataset):
        return len(dataset) // self.batch_size

    def step(self, session, inputs, input_seq_lengths, target_seq_lengths,
             target_vals, target_indices, forward_only=False):
        """
        Returns:
        ctc_loss, None
        ctc_loss, None
        """
        input_feed = {self.inputs.name: np.array(inputs), self.input_seq_lengths.name: np.array(input_seq_lengths),
                      self.target_seq_lengths.name: np.array(target_seq_lengths),
                      self.target_indices.name: np.array(target_indices), self.target_vals.name: target_vals}
        # Base output is ctc_loss and mean_loss
        output_feed = [self.ctc_loss, self.mean_loss]
        # If a tensorboard dir is configured then we add an merged_summaries operation
        if self.tensorboard_dir is not None:
            if forward_only:
                output_feed.append(self.test_summaries)
            else:
                output_feed.append(self.train_summaries)
        # If we are in training then we add the update operation
        if not forward_only:
            output_feed.append(self.update)
        outputs = session.run(output_feed, input_feed)
        if self.tensorboard_dir is not None:
            self.summary_writer.add_summary(outputs[2], self.global_step.eval())
        return outputs[0], outputs[1]

    def process_input(self, session, inputs, input_seq_lengths):
        """
        Returns:
          Translated text
        """
        input_feed = {self.inputs.name: np.array(inputs), self.input_seq_lengths.name: np.array(input_seq_lengths)}
        output_feed = [self.prediction]
        outputs = session.run(output_feed, input_feed)
        return outputs[0]

    def run_checkpoint(self, sess, checkpoint_dir, test_set, parent_test_conn=None):
        num_test_batches = self.getNumBatches(test_set)
        test_batch_pointer = 0

        # Save the model
        checkpoint_path = os.path.join(checkpoint_dir, "acousticmodel.ckpt")
        self.saver.save(sess, checkpoint_path, global_step=self.global_step)

        # Run a test set against the current model
        if num_test_batches > 0:
            if parent_test_conn is not None:
                # begin loading test data async
                # (uses different pipeline than train data)
                async_test_loader = Process(
                    target=self.getBatch,
                    args=(test_set, test_batch_pointer, False))
                async_test_loader.start()

            print(num_test_batches)
            for i in range(num_test_batches):
                print("On {0}th training iteration".format(i))
                if parent_test_conn is not None:
                    eval_inputs = parent_test_conn.recv()
                    # tell audio processor to go get another batch ready
                    # while we run last one through the graph
                    if i != num_test_batches - 1:
                        async_test_loader = Process(
                            target=self.getBatch,
                            args=(test_set, test_batch_pointer, False))
                        async_test_loader.start()
                else:
                    eval_inputs = self.getBatch(test_set, test_batch_pointer, False)

                test_batch_pointer = eval_inputs[5]

                _, step_loss = self.step(sess, eval_inputs[0], eval_inputs[1],
                                         eval_inputs[2], eval_inputs[3],
                                         eval_inputs[4], forward_only=True)
            print("\tTest: loss %.2f" % step_loss)
            sys.stdout.flush()

    def train(self, sess, test_set, train_set, steps_per_checkpoint, checkpoint_dir, async_get_batch, max_epoch=None):
        num_train_batches = self.getNumBatches(train_set)
        train_batch_pointer = 0
        parent_test_conn = parent_train_conn = None

        if async_get_batch:
            print("Setting up piplines to test and train data...")
            parent_train_conn, parent_test_conn = self.setConnections()
            async_train_loader = Process(
                target=self.getBatch,
                args=(train_set, train_batch_pointer, True))
            async_train_loader.start()

        step_time, mean_loss = 0.0, 0.0
        current_step = 0
        previous_loss = 0
        no_improvement_since = 0
        running = True
        while running:
            # begin timer
            start_time = time.time()
            if async_get_batch:
                # receive batch from pipe
                step_batch_inputs = parent_train_conn.recv()
                # begin fetching other batch while graph processes previous one
                async_train_loader = Process(
                    target=self.getBatch,
                    args=(train_set, train_batch_pointer, True))
                async_train_loader.start()
            else:
                step_batch_inputs = self.getBatch(train_set, train_batch_pointer, True)

            train_batch_pointer = step_batch_inputs[5]

            _, step_loss = self.step(sess, step_batch_inputs[0], step_batch_inputs[1],
                                     step_batch_inputs[2], step_batch_inputs[3],
                                     step_batch_inputs[4], forward_only=False)
            # Decrease learning rate if no improvement was seen over last 6 steps
            if step_loss >= previous_loss:
                no_improvement_since += 1
                if no_improvement_since == 6:
                    sess.run(self.learning_rate_decay_op)
                    no_improvement_since = 0
                    if self.learning_rate.eval() < 1e-7:
                        # End learning process
                        break
            else:
                no_improvement_since = 0
            previous_loss = step_loss

            print("Step {0} with loss {1}".format(current_step, step_loss))
            step_time += (time.time() - start_time) / steps_per_checkpoint
            mean_loss += step_loss / steps_per_checkpoint

            current_step += 1
            if (max_epoch is not None) and (current_step > max_epoch):
                # We have reached the maximum allowed, we should exit at the end of this run
                running = False

            # Check if we are at a checkpoint
            if current_step % steps_per_checkpoint == 0:
                print("global step %d learning rate %.4f step-time %.2f loss %.2f" %
                      (self.global_step.eval(), self.learning_rate.eval(), step_time, mean_loss))
                self.run_checkpoint(sess, checkpoint_dir, test_set, parent_test_conn)
                step_time, mean_loss = 0.0, 0.0

            # Shuffle the train set if we have done a full round over it
            if current_step % num_train_batches == 0:
                print("Shuffling the train set")
                shuffle(train_set)
