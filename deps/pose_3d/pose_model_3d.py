# -*- coding: utf-8 -*-

import os.path
import pkg_resources

import tensorflow as tf
from tensorflow import keras
# from tensorboard.plugins.beholder import Beholder
# from tensorflow.contrib.data.python.ops import prefetching_ops
import numpy as np

from .network import build_model, build_discriminator
from . import config
from . import utils
import tf_smpl
from tf_perspective_projection import project as proj


class PoseModel3d:
    def __init__(self,
                 input_shape,
                 graph=None,
                 mode='test',
                 dataset=None,
                 summary_dir='/tmp/tf_logs/3d_pose/',
                 saver_path='/tmp/tf_ckpts/3d_pose/ckpts/3d_pose.ckpt',
                 restore_model=True,
                 pose_loss=True,
                 reproject_loss=True,
                 mesh_loss=True,
                 smpl_model=None,
                 discriminator=False):
        self.graph = graph if graph is not None else tf.get_default_graph()
        with self.graph.as_default():
            tfconf = tf.ConfigProto()
            # tfconf.gpu_options.allow_growth = True  # pylint: disable=no-member
            self.sess = tf.Session(config=tfconf)
            # allow using Keras layers in network
            keras.backend.set_session(self.sess)

            self.img_dim = tf.constant([input_shape[2], input_shape[1]],
                                       dtype=tf.float32)
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
                self.in_placeholder = tf.placeholder_with_default(
                    self.next_input[0], input_shape)
                self.outputs = build_model(self.in_placeholder, training)

                self.pose_loss = pose_loss
                self.mesh_loss = mesh_loss
                self.reproject_loss = reproject_loss
                if self.mesh_loss or self.reproject_loss:
                    self.smpl = tf_smpl.batch_smpl.SMPL(smpl_model)
                    faces_path = pkg_resources.resource_filename(
                        tf_smpl.__name__, 'smpl_faces.npy')
                    faces = np.load(faces_path)
                    self.mesh_faces = tf.constant(faces, dtype=tf.int32)
                    self.vert_faces = tf.constant(
                        utils.vertex_faces_from_face_verts(faces))
                if self.discriminator:
                    if training:
                        d_in = tf.concat(
                            [self.next_input[1], self.outputs[:, :72]], axis=0)
                    else:
                        d_in = self.outputs
                    self.discriminator_outputs = build_discriminator(d_in)
            else:
                self.in_placeholder = tf.placeholder(tf.float32, input_shape)
                self.outputs = build_model(self.in_placeholder, training=False)

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
                                       {'in': self.in_placeholder},
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

    def estimate(self, input_inst):
        """ Run the model on an input instance """
        with self.graph.as_default():
            if self.saver is None:
                self.saver = tf.train.Saver()
            if self.restore:
                self.restore_from_checkpoint()
            out = self.sess.run(
                self.outputs,
                feed_dict={self.in_placeholder: input_inst})
        return out

    def get_encoder_losses(self, out_pose, gt_pose, betas, gt_joints2d):
        with self.graph.as_default():
            total_loss = 0

            with tf.variable_scope("rodrigues"):
                out_mat = proj.rodrigues_batch(tf.reshape(out_pose, [-1, 3]))
                gt_mat = proj.rodrigues_batch(tf.reshape(gt_pose, [-1, 3]))

            if self.pose_loss:
                # Switch to L1 loss?
                pose_loss_direct = tf.losses.mean_squared_error(
                    labels=gt_pose, predictions=out_pose,
                    weights=config.pose_loss_direct_scale)
                tf.summary.scalar('pose_loss_direct', pose_loss_direct,
                                family='losses')
                pose_loss = tf.losses.mean_squared_error(
                    labels=gt_mat, predictions=out_mat,
                    weights=config.pose_loss_scale)
                tf.summary.scalar('pose_loss', pose_loss, family='losses')
                total_loss += pose_loss

            out_pose_norm = tf.norm(tf.reshape(out_pose, [-1, 3]), axis=1)
            out_pose_zeros = tf.zeros_like(out_pose_norm, tf.float32)
            out_pose_too_large = tf.where(out_pose_norm > config.joint_limit,
                                          out_pose_norm, out_pose_zeros)
            reg_loss = tf.losses.mean_squared_error(
                labels=out_pose_zeros, predictions=out_pose_too_large,
                weights=config.reg_loss_scale)
            tf.summary.scalar('reg_loss', reg_loss, family='losses')
            total_loss += reg_loss

            if self.mesh_loss or self.reproject_loss:
                # 3D mesh loss uses ground truth global rotation
                out_pose_gt_global = tf.concat(
                    [gt_pose[:, :3], out_pose[:, 3:]], axis=1)
                out_meshes, _, _ = self.smpl(betas, out_pose_gt_global,
                                             get_skin=True)
                out_joints = self.smpl.J_transformed
                gt_meshes, _, _ = self.smpl(betas, gt_pose, get_skin=True)
                gt_joints3d = self.smpl.J_transformed

            if self.mesh_loss:
                mesh_loss = tf.losses.mean_squared_error(
                    labels=gt_meshes, predictions=out_meshes,
                    weights=config.mesh_loss_scale)
                joint_loss = tf.losses.mean_squared_error(
                    labels=gt_joints3d, predictions=out_joints,
                    weights=config.joint_loss_scale)
                loss3d = mesh_loss + joint_loss
                tf.summary.scalar('3d_loss', loss3d, family='losses')
                total_loss += loss3d

            if self.reproject_loss:
                # Flip GT 2D y-axis since it is in image coordinates
                gt_joints2d *= tf.constant([1.0, -1.0])
                gt_joints2d += tf.stack([0., self.img_dim[1]])
                # For now only estimate camera position z and rotation about y
                out_cam_pos = self.outputs[:, 72:75]
                # out_cam_pos = out_cam_pos * pos_mask
                # pos_mask = tf.constant([0., 0., 1.])[tf.newaxis]
                out_cam_p_tile = tf.reshape(
                    tf.tile(out_cam_pos, [1, config.n_joints_smpl]), [-1, 3])
                out_cam_rot = self.outputs[:, 75:78]
                # rot_mask = tf.constant([0., 1., 0.])[tf.newaxis]
                # out_cam_rot = out_cam_rot * rot_mask
                out_cam_r_tile = tf.reshape(
                    tf.tile(out_cam_rot, [1, config.n_joints_smpl]), [-1, 3])
                out_cam_f = self.outputs[:, 78]
                out_cam_f = tf.tile([config.fl], [tf.shape(self.outputs)[0]])
                out_cam_f_tile = tf.reshape(
                    tf.tile(out_cam_f[:, None], [1, config.n_joints_smpl]),
                    [-1])

                # 2D reprojection loss - uses predicted camera parameters to
                # project both GT and predicted 3D: capped unless low
                # Stop gradients since this loss should only affect pose preds
                cam_params_tile_sg = (
                    tf.stop_gradient(out_cam_p_tile),
                    tf.stop_gradient(out_cam_r_tile),
                    tf.stop_gradient(out_cam_f_tile))
                with tf.variable_scope("projection"):
                    # reshape from (batch, j, 3) to (batch * j, 3)
                    out_2d = proj.project(
                        tf.reshape(out_joints, [-1, 3]), *cam_params_tile_sg)
                    gt_2d_out_cam = proj.project(
                        tf.reshape(gt_joints3d, [-1, 3]), *cam_params_tile_sg)
                # Rescale to image size
                out_2d = out_2d / config.ss * self.img_dim[1] + self.img_dim/2
                gt_2d_out_cam = (gt_2d_out_cam / config.ss * self.img_dim[1]
                                 + self.img_dim/2)
                # joints2d reshape from (batch, j, 2) to (batch * j, 2)
                reproj_loss = tf.losses.mean_squared_error(
                    labels=gt_2d_out_cam, predictions=out_2d,
                    weights=config.reproj_loss_scale)
                tf.summary.scalar('reproj_loss', reproj_loss, family='losses')
                reproj_loss = tf.minimum(1 / config.n_joints_smpl, reproj_loss)
                total_loss += reproj_loss

                # Camera loss - compares reprojected GT 3D using predicted
                # camera to GT 2D locations: capped unless low (< 0.5 of img)
                with tf.variable_scope("projection"):
                    out_2d_gt_pose = proj.project(
                        tf.reshape(gt_joints3d, [-1, 3]),
                        out_cam_p_tile, out_cam_r_tile, out_cam_f_tile)
                # Rescale to image size
                out_2d_gt_pose = (out_2d_gt_pose / config.ss * self.img_dim[1]
                                  + self.img_dim/2)
                # joints2d reshape from (batch, j, 2) to (batch * j, 2)
                cam_loss = tf.losses.mean_squared_error(
                    labels=tf.reshape(gt_joints2d, [-1, 2]),
                    predictions=out_2d_gt_pose, weights=config.cam_loss_scale)
                tf.summary.scalar('cam_loss', cam_loss, family='losses')
                cam_loss = tf.minimum(0.5, cam_loss)

                # Camera angle loss - compare reprojected GT 3D using predicted
                # camera to GT 2D locations using a ray method
                # See https://arxiv.org/pdf/1808.04999.pdf
                # Switch to L1 loss?
                vec_to_gt_3d = gt_joints3d - out_cam_pos[:, tf.newaxis, :]
                vec_to_gt_3d = tf.nn.l2_normalize(vec_to_gt_3d, axis=2)
                gt_2d_in_3d = utils.get_2d_points_in_3d(
                    gt_joints2d, out_cam_rot, out_cam_f, self.img_dim)
                vec_to_gt_2d = tf.nn.l2_normalize(gt_2d_in_3d, axis=2)
                dot = tf.reduce_sum(vec_to_gt_3d * vec_to_gt_2d, axis=2)
                # Dot product can be out of [-1, 1] because of normalize eps
                dot = tf.clip_by_value(dot, -1.0 + 1e-6, 1.0 - 1e-6)
                angle_diff = tf.acos(dot)
                cam_angle_loss = tf.losses.mean_squared_error(
                    labels=tf.zeros_like(angle_diff), predictions=angle_diff,
                    weights=config.cam_angle_loss_scale)
                tf.summary.scalar('cam_angle', cam_angle_loss, family='losses')

                # Camera regularisation
                # camera position -5 < z < 0.3, camera focal length > 0
                cam_limits = tf.gather(self.outputs, [74, 74, 78], axis=1)
                cam_limits *= tf.constant([-1.0, 1.0, 1.0])[tf.newaxis]
                cam_limits += tf.constant([-0.3, 10.0, 0.0])
                cam_zeros = tf.zeros_like(cam_limits)
                cam_neg = tf.where(tf.less(cam_limits, cam_zeros),
                                   cam_limits, cam_zeros)
                cam_neg_loss = tf.losses.mean_squared_error(
                    labels=cam_zeros, predictions=cam_neg)
                # penalize rotations that are out of field of view
                cam_rot_norm = tf.norm(tf.reshape(out_cam_rot, [-1, 3]),
                                       axis=1)
                cam_rot_zeros = tf.zeros_like(cam_rot_norm, tf.float32)
                cam_rot_too_large = tf.where(cam_rot_norm > np.pi * 0.125,
                                             cam_rot_norm, cam_rot_zeros)
                cam_rot_loss = tf.losses.mean_squared_error(
                    labels=cam_rot_zeros, predictions=cam_rot_too_large,
                    weights=10.0)
                cam_reg_losses = cam_rot_loss + cam_neg_loss
                total_loss += cam_loss + cam_angle_loss + cam_reg_losses

                # 3D points should be in positive half-space of camera plane
                # Currently not active (not added to total loss)
                cam_plane_n, cam_plane_d = utils.get_camera_normal_plane(
                    out_cam_pos, out_cam_rot)
                p_dot_n = tf.reduce_sum(
                    cam_plane_n[:, tf.newaxis, :] * gt_joints3d, axis=1)
                plane_zeros = tf.zeros_like(p_dot_n) + config.fl * 10.0
                cam_plane_neg = tf.where(
                    p_dot_n < cam_plane_d[:, tf.newaxis],
                    p_dot_n, plane_zeros)
                cam_plane_loss = tf.losses.mean_squared_error(
                    labels=plane_zeros, predictions=cam_plane_neg)
                # tf.summary.scalar('cam_aux', cam_plane_loss, family='losses')

                # Camera position summary scalars
                pos_diff = tf.reduce_mean(tf.norm(out_cam_pos, axis=1), axis=0)
                rot_dot = tf.reduce_sum(
                    cam_plane_n * tf.nn.l2_normalize(-out_cam_pos, axis=1),
                    axis=1)
                rot_dot = tf.reduce_mean(rot_dot, axis=0)
                tf.summary.scalar('cam_pos_diff', pos_diff, family='camera')
                tf.summary.scalar('rot_dot', rot_dot, family='camera')

                # Render view from camera and output mesh for visualisation
                with tf.variable_scope("render"):
                    view = (out_cam_pos, out_cam_rot,
                            tf.atan2(config.ss / 2, out_cam_f) * 360 / np.pi,
                            self.mesh_faces, self.vert_faces,
                            out_cam_pos[:, tf.newaxis, :])
                    view_f = (tf.constant([0.0, -0.35, -4.0]),
                              tf.constant([0., 0., 0.]), 30.0, self.mesh_faces,
                              self.vert_faces, tf.constant([0.0, 1.0, -4.0]))
                    render_cam = utils.render_mesh_verts_cam(gt_meshes, *view)
                    render_out = utils.render_mesh_verts_cam(out_meshes,
                                                             *view_f)
                tf.summary.image('camera_view', render_cam, max_outputs=1)
                tf.summary.image('out_mesh', render_out, max_outputs=1)

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
            self.dataset = self.dataset.prefetch(16)
            # self.dataset = self.dataset.apply(
            #     prefetching_ops.copy_to_device("/gpu:0")).prefetch(1)
            iterator = self.dataset.make_initializable_iterator()
            train_handle = self.sess.run(iterator.string_handle())

            _, gt_pose, betas, gt_joints2d, zrot = self.next_input
            with tf.variable_scope("rotate_global"):
                gt_pose = utils.rotate_global_pose(gt_pose, zrot)
            out_pose = self.outputs[:, :72]

            total_loss = self.get_encoder_losses(
                out_pose, gt_pose, betas, gt_joints2d)

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
                    gs = tf.train.global_step(self.sess, self.step)
                    try:
                        if self.discriminator:
                            _, summary_eval, _ = self.sess.run(
                                (train, summary, train_discriminator),
                                feed_dict=feed)
                        else:
                            _, summary_eval = self.sess.run(
                                (train, summary), feed_dict=feed)
                    except tf.errors.OutOfRangeError:
                        break
                    print("\r{:7}".format(gs), end=' ', flush=True)
                    if gs % 10 == 0:
                        self.summary_writer.add_summary(summary_eval, gs)
                        # self.beholder.update(session=self.sess)
                    if gs % 2000 == 0:
                        self.saver.save(self.sess, self.saver_path,
                                        global_step=self.step)
                self.saver.save(self.sess, self.saver_path,
                                global_step=self.step)

    def evaluate(self):
        """ Evaluate the dataset passed in at the model creation time """
        with self.graph.as_default():
            iterator = self.dataset.make_initializable_iterator()
            eval_handle = self.sess.run(iterator.string_handle())

            _, gt_pose, betas, gt_joints2d, _ = self.next_input

            out_pose = self.outputs[:, :72]
            pose_error = tf.losses.mean_squared_error(
                labels=gt_pose, predictions=out_pose)

            _ = self.smpl(betas, out_pose, get_skin=False)
            out_joints = self.smpl.J_transformed
            out_cam_pos = tf.tile(self.outputs[:, 72:75],
                                  [1, config.n_joints_smpl])
            out_cam_rot = tf.tile(self.outputs[:, 75:78],
                                  [1, config.n_joints_smpl])
            out_cam_f = tf.tile(self.outputs[:, 78],
                                [1, config.n_joints_smpl])
            # Flip y-axis since it is in image coordinates
            gt_joints2d *= tf.constant([1.0, -1.0])
            gt_joints2d += tf.stack([0., self.img_dim[1]])
            with tf.variable_scope("projection"):
                # reshape from (batch, j, 3) to (batch * j, 3)
                out_2d = proj.project(tf.reshape(out_joints, [-1, 3]),
                                      tf.reshape(out_cam_pos, [-1, 3]),
                                      tf.reshape(out_cam_rot, [-1, 3]),
                                      tf.reshape(out_cam_f, [-1]))
            # Rescale to image size
            out_2d = out_2d * self.img_dim[1] + self.img_dim / 2
            # joints2d reshape from (batch, j, 2) to (batch * j, 2)
            reproj_error = tf.losses.mean_squared_error(
                labels=tf.reshape(gt_joints2d, [-1, 2]), predictions=out_2d)

            self.sess.run(iterator.initializer)
            feed = {self.input_handle: eval_handle}
            all_pose_errors = []
            all_reproj_errors = []
            while True:
                try:
                    pose_error_eval, reproj_error_eval = self.sess.run(
                        (pose_error, reproj_error),
                        feed_dict=feed)
                    all_pose_errors.append(pose_error_eval)
                    all_reproj_errors.append(reproj_error_eval)
                except tf.errors.OutOfRangeError:
                    break
            all_pose_errors = np.array(all_pose_errors)
            all_reproj_errors = np.array(all_reproj_errors)
            return all_pose_errors, all_reproj_errors
