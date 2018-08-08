import os.path
import pkg_resources

import tensorflow as tf
from tensorflow import keras
from tensorboard.plugins.beholder import Beholder
import numpy as np

from .network import build_model, build_discriminator
from . import config
from . import utils
import tf_smpl
from tf_perspective_projection.project import rodrigues_batch, project


class PoseModel3d:
    def __init__(self,
                 input_shape,
                 graph=None,
                 mode='test',
                 dataset=None,
                 summary_dir='/tmp/tf_logs/3d_pose/',
                 saver_path='/tmp/tf_ckpts/3d_pose/ckpts/3d_pose.ckpt',
                 restore_model=True,
                 reproject_loss=True,
                 mesh_loss=True,
                 smpl_model=None,
                 discriminator=False):
        self.graph = graph if graph != None else tf.get_default_graph()
        with self.graph.as_default():
            tfconf = tf.ConfigProto()
            tfconf.gpu_options.allow_growth = True  # pylint: disable=no-member
            self.sess = tf.Session(config=tfconf)
            # allow using Keras layers in network
            keras.backend.set_session(self.sess)

            shorter_side = min(input_shape[1], input_shape[2])
            self.img_side_len = shorter_side
            self.discriminator = discriminator
            if mode not in ['train', 'test', 'eval']:
                raise ValueError("mode must be 'train', 'test', or 'eval'")
            training = mode == 'train'
            if mode == 'train' or mode == 'eval':
                self.dataset = dataset
                self.input_handle = tf.placeholder(tf.string, shape=[])
                iterator = tf.data.Iterator.from_string_handle(
                    self.input_handle,
                    self.dataset.output_types, self.dataset.output_shapes)
                self.next_input = iterator.get_next()
                # placeholders for shape inference
                self.in_placehold = tf.placeholder_with_default(
                    self.next_input[0], input_shape)
                self.in_placehold_loc = tf.placeholder_with_default(
                    self.next_input[1], [None, config.n_joints, 3])
                self.outputs = build_model(
                    self.in_placehold, self.in_placehold_loc, training)
                self.mesh_loss = mesh_loss
                self.reproject_loss = reproject_loss
                if self.mesh_loss or self.reproject_loss:
                    self.smpl = tf_smpl.batch_smpl.SMPL(smpl_model)
                    faces_path = pkg_resources.resource_filename(
                        tf_smpl.__name__, 'smpl_faces.npy')
                    self.mesh_faces = tf.constant(np.load(faces_path),
                                                  dtype=tf.int32)
                if self.discriminator:
                    if training:
                        d_in = tf.concat(
                            [self.next_input[2], self.outputs[:, :72]], 0)
                    else:
                        d_in = self.outputs
                    self.discriminator_outputs = build_discriminator(d_in)
            else:
                self.in_placehold = tf.placeholder(tf.float32, input_shape)
                self.in_placehold_loc = tf.placeholder(
                    tf.float32, [None, config.n_joints, 3])
                self.outputs = build_model(
                    self.in_placehold, self.in_placehold_loc, training=False)

            self.step = tf.train.get_or_create_global_step()

            self.saver_path = saver_path
            self.saver = None

            subdir = 'train' if training else 'test'
            self.summary_writer = tf.summary.FileWriter(
                os.path.join(summary_dir, subdir), self.graph)
            # self.beholder = Beholder(os.path.join(summary_dir, subdir))

            self.sess.run(tf.global_variables_initializer())

            self.restore = restore_model
            self.already_restored = False

    def save_model(self, save_model_path: str):
        """ Save the model as a tf.SavedModel """
        with self.graph.as_default():
            tf.saved_model.simple_save(self.sess, save_model_path,
                                       {'in': self.in_placehold,
                                        'in_loc': self.in_placehold_loc},
                                       {'out': self.outputs})

    def restore_from_checkpoint(self):
        """ Restore weights from checkpoint - only runs once """
        with self.graph.as_default():
            # terminal colours for printing
            ok_col, warn_col, normal_col = '\033[92m', '\033[93m', '\033[0m'
            if self.already_restored:
                print("{}Already restored{}".format(warn_col, normal_col))
                return
            restore_path = os.path.dirname(self.saver_path)
            restore_ckpt = tf.train.latest_checkpoint(restore_path)
            if restore_ckpt != None:
                try:
                    self.saver.restore(self.sess, restore_ckpt)
                    print(
                        "{}Model restored from checkpoint at {}{}".format(
                        ok_col, self.saver_path, normal_col))
                    self.already_restored = True
                except:
                    print(
                        "{}Invalid model checkpoint found for given path {}"
                        "\nContinuing without loading model{}".format(
                        warn_col, self.saver_path, normal_col))
            else:
                print("{}No model checkpoint found for given path {}"
                        "\nContinuing without loading model{}".format(
                        warn_col, self.saver_path, normal_col))

    def estimate(self, input_inst, input_loc):
        """ Run the model on an input instance """
        with self.graph.as_default():
            if self.saver is None:
                self.saver = tf.train.Saver()
            if self.restore:
                self.restore_from_checkpoint()
            out = self.sess.run(
                self.outputs,
                feed_dict={self.in_placehold: input_inst,
                           self.in_placehold_loc: input_loc})
        return out

    def get_encoder_losses(self, out_pose, gt_pose, betas, smpl_joints2d):
        with self.graph.as_default():
            with tf.variable_scope("rodrigues"):
                out_mat = rodrigues_batch(tf.reshape(out_pose, [-1, 3]))
                gt_mat = rodrigues_batch(tf.reshape(gt_pose, [-1, 3]))

            pose_loss_direct = tf.losses.mean_squared_error(
                labels=gt_pose, predictions=out_pose,
                weights=config.pose_loss_direct_scale)
            tf.summary.scalar('pose_loss_direct', pose_loss_direct,
                              family='losses')
            pose_loss = tf.losses.mean_squared_error(
                labels=gt_mat, predictions=out_mat,
                weights=config.pose_loss_scale)
            tf.summary.scalar('pose_loss', pose_loss, family='losses')

            out_pose_norm = tf.norm(tf.reshape(out_pose, [-1, 3]), axis=1)
            out_pose_norm_zeros = tf.zeros(tf.shape(out_pose_norm))
            out_pose_too_large = tf.where(
                tf.greater(out_pose_norm, config.reg_joint_limit),
                out_pose_norm, out_pose_norm_zeros)
            reg_loss = tf.losses.mean_squared_error(
                labels=out_pose_norm_zeros, predictions=out_pose_too_large,
                weights=config.reg_loss_scale)
            tf.summary.scalar('reg_loss', reg_loss, family='losses')

            total_loss = pose_loss + reg_loss

            if self.mesh_loss or self.reproject_loss:
                # TODO: 3D mesh loss uses ground truth global rotation
                out_pose_gt_global = tf.concat(
                    [gt_pose[:, :3], out_pose[:, 3:]], axis=1)
                out_meshes, _, _ = self.smpl(betas, out_pose_gt_global,
                                             get_skin=True)
                out_joints = self.smpl.J_transformed
                gt_meshes, _, _ = self.smpl(betas, gt_pose, get_skin=True)
                gt_joints = self.smpl.J_transformed

            if self.mesh_loss:
                mesh_loss = tf.losses.mean_squared_error(
                    labels=gt_meshes, predictions=out_meshes,
                    weights=config.mesh_loss_scale)
                joint_loss = tf.losses.mean_squared_error(
                    labels=gt_joints, predictions=out_joints,
                    weights=config.joint_loss_scale)
                loss3d = mesh_loss + joint_loss
                tf.summary.scalar('3d_loss', loss3d, family='losses')
                total_loss += loss3d

            if self.reproject_loss:
                out_cam_pos = tf.tile(self.outputs[:, 72:75],
                                      [1, config.n_joints_smpl])
                out_cam_rot = tf.tile(self.outputs[:, 75:78],
                                      [1, config.n_joints_smpl])
                out_cam_foc = tf.tile(self.outputs[:, 78, tf.newaxis],
                                      [1, config.n_joints_smpl])
                with tf.variable_scope("projection"):
                    # reshape from (batch, j, 3) to (batch * j, 3)
                    out_2d = project(tf.reshape(out_joints, [-1, 3]),
                                     tf.reshape(out_cam_pos, [-1, 3]),
                                     tf.reshape(out_cam_rot, [-1, 3]),
                                     tf.reshape(out_cam_foc, [-1]))
                # Rescale to image size
                out_2d = (out_2d + 1) * self.img_side_len * 0.5
                # joints2d reshape from (batch, j, 2) to (batch * j, 2)
                reproj_loss = tf.losses.huber_loss(
                    labels=tf.reshape(smpl_joints2d, [-1, 2]),
                    predictions=out_2d, delta=self.img_side_len/15,
                    weights=config.reproj_loss_scale)
                tf.summary.scalar('reproj_loss', reproj_loss, family='losses')
                total_loss += reproj_loss
                with tf.variable_scope("projection"):
                    out_2d_gt_pose = project(tf.reshape(gt_joints, [-1, 3]),
                                             tf.reshape(out_cam_pos, [-1, 3]),
                                             tf.reshape(out_cam_rot, [-1, 3]),
                                             tf.reshape(out_cam_foc, [-1]))
                # Rescale to image size
                out_2d_gt_pose = (out_2d_gt_pose + 1) * self.img_side_len * 0.5
                # joints2d reshape from (batch, j, 2) to (batch * j, 2)
                cam_loss = tf.losses.huber_loss(
                    labels=tf.reshape(smpl_joints2d, [-1, 2]),
                    predictions=out_2d_gt_pose, delta=self.img_side_len/15,
                    weights=config.cam_loss_scale)
                tf.summary.scalar('cam_loss', cam_loss, family='losses')
                # camera axis y-component should point up and focal length > 0
                cam_limits = tf.gather(self.outputs, [76, 78], axis=1)
                cam_zeros = tf.zeros(tf.shape(cam_limits))
                cam_not_positive = tf.where(
                    tf.less(cam_limits, cam_zeros), cam_limits, cam_zeros)
                cam_loss_not_positive = tf.losses.mean_squared_error(
                    labels=cam_zeros, predictions=cam_not_positive)
                total_loss += cam_loss + cam_loss_not_positive

                with tf.variable_scope("render"):
                    render = utils.render_mesh_verts_cam(
                        gt_meshes, 
                        self.outputs[:, 72:75], self.outputs[:, 75:78],
                        tf.atan2(1.0, self.outputs[:, 78]) * 360 / np.pi,
                        self.mesh_faces)
                    # lights = tf.constant([[-2., 0., 0.], [0., -2., 0.],
                    #                       [0., 0., -2.], [-1., -1., -1.]])
                    render_outs = utils.render_mesh_verts_cam(
                        out_meshes, tf.constant([0.0, 0.0, -3.5]),
                        tf.constant([0., 0., 0.]), 40.0, self.mesh_faces,
                        lights=None)
                tf.summary.image('silhouettes', render, max_outputs=1)
                tf.summary.image('output_meshes', render_outs, max_outputs=1)

            return total_loss

    def get_discriminator_loss(self, out_pose, gt_pose):
        with self.graph.as_default():
            # First half is real (gt), second half is fake (predictions)
            # First output true, second output false
            batch_size = tf.shape(self.discriminator_outputs)[0] / 2
            real_labels = tf.reshape(tf.tile([1, 0], [batch_size]),
                                     [batch_size, 2])
            fake_labels = tf.reshape(tf.tile([0, 1], [batch_size]), 
                                     [batch_size, 2])
            disc_loss = tf.losses.softmax_cross_entropy(
                onehot_labels=tf.concat([real_labels, fake_labels], axis=0),
                logits=self.discriminator_outputs)
            tf.summary.scalar('discriminator_loss', disc_loss,
                              family='discriminator')

            disc_pred_out = self.discriminator_outputs[batch_size:]
            disc_enc_loss = tf.losses.softmax_cross_entropy(
                onehot_labels=real_labels,
                logits=disc_pred_out, weights=config.disc_loss_scale)
            tf.summary.scalar('discriminator_loss', disc_enc_loss, 
                              family='losses')
            return disc_loss, disc_enc_loss

    def train(self, batch_size: int, epochs: int):
        """ Train the model using the dataset passed in at model creation """
        with self.graph.as_default():
            self.dataset = self.dataset.shuffle(batch_size * 96)
            self.dataset = self.dataset.batch(batch_size)
            self.dataset = self.dataset.prefetch(20)
            # self.dataset = self.dataset.apply(
            #     tf.contrib.data.copy_to_device("/gpu:0")).prefetch(1)
            iterator = self.dataset.make_initializable_iterator()
            train_handle = self.sess.run(iterator.string_handle())

            _, _, gt_pose, betas, smpl_joints2d = self.next_input
            with tf.variable_scope("rotate_global"):
                gt_pose = utils.rotate_global_pose(gt_pose)
            out_pose = self.outputs[:, :72]

            total_loss = self.get_encoder_losses(
                out_pose, gt_pose, betas, smpl_joints2d)

            if self.discriminator:
                disc_total_loss, disc_enc_loss = self.get_discriminator_loss(
                    out_pose, gt_pose)
                disc_optimizer = tf.train.AdamOptimizer(learning_rate=4e-4)
                discriminator_vars = tf.get_collection(
                    tf.GraphKeys.TRAINABLE_VARIABLES, scope='discriminator')
                train_discriminator = disc_optimizer.minimize(
                    disc_total_loss, var_list=discriminator_vars)
                self.sess.run(tf.variables_initializer(
                    disc_optimizer.variables()))
                total_loss += disc_enc_loss

            tf.summary.scalar('total_loss', total_loss, family='losses')
            summary = tf.summary.merge_all()

            optimizer = tf.train.AdamOptimizer(learning_rate=2e-4)
            encoder_vars = tf.get_collection(
                tf.GraphKeys.TRAINABLE_VARIABLES, scope='encoder')
            update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
            with tf.control_dependencies(update_ops):
                train = optimizer.minimize(
                    total_loss * config.total_loss_scale,
                    global_step=self.step, var_list=encoder_vars)
            self.sess.run(tf.variables_initializer(optimizer.variables()))

            if self.saver is None:
                self.saver = tf.train.Saver()
            if self.restore:
                self.restore_from_checkpoint()

            self.summary_writer.add_graph(self.graph)

            # Train loop
            for _ in range(epochs):
                self.sess.run(iterator.initializer)
                feed = {self.input_handle: train_handle}
                while True:
                    try:
                        gs = tf.train.global_step(self.sess, self.step)
                        if self.discriminator:
                            _, summary_eval, _ = self.sess.run(
                                (train, summary, train_discriminator),
                                feed_dict=feed)
                        else:
                            _, summary_eval = self.sess.run(
                                (train, summary), feed_dict=feed)
                        self.summary_writer.add_summary(summary_eval, gs)
                        # self.beholder.update(session=self.sess)
                    except tf.errors.OutOfRangeError:
                        break
                    print("{:7}".format(gs), end=' ', flush=True)
                    if gs % 2000 == 0:
                        self.saver.save(self.sess, self.saver_path,
                                        global_step=self.step)
                self.saver.save(self.sess, self.saver_path,
                                global_step=self.step)

    def evaluate(self):
        """ Evaluate the dataset passed in at the model creation time """
        with self.graph.as_default():
            iterator = self.dataset.make_initializable_iterator()
            train_handle = self.sess.run(iterator.string_handle())

            _, _, gt_pose, betas, smpl_joints2d = self.next_input

            out_pose = self.outputs[:, :72]
            pose_loss = tf.losses.mean_squared_error(
                labels=gt_pose, predictions=out_pose)

            _ = self.smpl(betas, out_pose, get_skin=False)
            out_joints = self.smpl.J_transformed
            out_cam_pos = tf.tile(self.outputs[:, 72:75],
                                    [1, config.n_joints_smpl])
            out_cam_rot = tf.tile(self.outputs[:, 75:78],
                                    [1, config.n_joints_smpl])
            out_cam_foc = tf.tile(self.outputs[:, 78, tf.newaxis],
                                    [1, config.n_joints_smpl])
            with tf.variable_scope("projection"):
                # reshape from (batch, j, 3) to (batch * j, 3)
                out_2d = project(tf.reshape(out_joints, [-1, 3]),
                                 tf.reshape(out_cam_pos, [-1, 3]),
                                 tf.reshape(out_cam_rot, [-1, 3]),
                                 tf.reshape(out_cam_foc, [-1]))
            # Rescale to image size
            out_2d = (out_2d + 1) * self.img_side_len * 0.5
            # joints2d reshape from (batch, j, 2) to (batch * j, 2)
            reproj_loss = tf.losses.mean_squared_error(
                labels=tf.reshape(smpl_joints2d, [-1, 2]), predictions=out_2d)

            self.sess.run(iterator.initializer)
            feed = {self.input_handle: train_handle}
            all_pose_losses = []
            all_reproj_losses = []
            while True:
                try:
                    pose_loss_eval, reproj_loss_eval = self.sess.run(
                        (pose_loss, reproj_loss),
                        feed_dict=feed)
                    all_pose_losses.append(pose_loss_eval)
                    all_reproj_losses.append(reproj_loss_eval)
                except tf.errors.OutOfRangeError:
                    break
            all_pose_losses = np.array(all_pose_losses)
            all_reproj_losses = np.array(all_reproj_losses)
            return all_pose_losses, all_reproj_losses
