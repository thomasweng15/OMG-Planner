# --------------------------------------------------------
# Licensed under The MIT License [see LICENSE for details]
# --------------------------------------------------------

from .optimizer import Optimizer
from .cost import Cost
from .util import *
from .online_learner import Learner

from . import config
import time
import multiprocessing
from copy import deepcopy

import torch
from liegroups.torch import SE3
import pytransform3d.rotations as pr
import pytransform3d.transformations as pt

from bullet.utils import draw_pose
from .viz_trimesh import visualize_predicted_grasp, trajT_to_grasppredT, grasppredT_to_trajT
import pytorch3d.transforms as ptf
import pybullet as p
from manifold_grasping.control_pts import *

# def visualize_predicted_grad(iter, cfg, T_obj2ee, out_lm, T_objfrm2obj, show=False, rotate_wrist=False):
#     grasp_root = f"/checkpoint/thomasweng/acronym/grasps"
#     obj_name = 'Book_5e90bf1bb411069c115aef9ae267d6b7_0.0268818133810836'
#     obj_mesh = load_mesh(f"{grasp_root}/{obj_name}.h5", mesh_root_dir=cfg.acronym_dir)

#     obj_mesh = obj_mesh.apply_transform(np.linalg.inv(T_objfrm2obj))

#     # if rotate_wrist:
#     #     T_obj2ee = trajT_to_grasppredT(T_obj2ee)
#     #     T_obj2goal = trajT_to_grasppredT(T_obj2goal)

#     ee_pose = [create_gripper_marker(color=[0, 0, 255]).apply_transform(T_obj2ee)]

#     # Visualize grad
#     # goal_pose = [create_gripper_marker(color=[255, 0, 0]).apply_transform(T_obj2goal)]
#     grad_pose = ee_pose

#     Stheta_ee2goal = out_lm.cpu()

#     # loss = torch.linalg.norm(out_lm)
#     loss = torch.linalg.norm(Stheta_ee2goal)
#     loss.backward()
#     Sthetadot_body = -batch_x.grad.squeeze().cpu().numpy()

#     scene = trimesh.Scene([obj_mesh] + ee_pose + grad_pose)
#     if not show:
#         if not os.path.exists(f'{cfg.exp_dir}/{cfg.exp_name}/{cfg.scene_file}/pred_pkls'):
#             os.mkdir(f'{cfg.exp_dir}/{cfg.exp_name}/{cfg.scene_file}/pred_pkls')
#         fname = f'{cfg.exp_dir}/{cfg.exp_name}/{cfg.scene_file}/pred_pkls/pred_{iter}.pkl'
#         export = trimesh.exchange.export.export_scene(scene, file_obj=None, file_type='dict')
#         pickle.dump(export, open(fname, 'wb'))
#     else:
#         scene.show()
#     import IPython; IPython.embed()


def solve_one_pose_ik(input):
    """
    solve for one ik
    """
    (
        end_pose,
        standoff_grasp,
        one_trial,
        init_seed,
        attached,
        reach_tail_len,
        ik_seed_num,
        use_standoff,
        seeds,
    ) = input

    r = config.cfg.ROBOT
    joint = 0.04
    finger_joint = np.array([joint, joint])
    finger_joints = np.tile(finger_joint, (reach_tail_len, 1))
    reach_goal_set = []
    standoff_goal_set = []
    any_ik = False

    for seed in seeds:
        if use_standoff:
            standoff_pose = pack_pose(standoff_grasp[-1])
            standoff_ik = r.inverse_kinematics(
                standoff_pose[:3], ros_quat(standoff_pose[3:]), seed=seed
            )  #
            standoff_iks = [standoff_ik]  # this one can often be off

            if standoff_ik is not None:
                for k in range(reach_tail_len):
                    standoff_pose = pack_pose(standoff_grasp[k])
                    standoff_ik_k = r.inverse_kinematics(
                        standoff_pose[:3],
                        ros_quat(standoff_pose[3:]),
                        seed=standoff_iks[-1],
                    )  #
                    if standoff_ik_k is not None:
                        standoff_iks.append(np.array(standoff_ik_k))
                    else:
                        break
            standoff_iks = standoff_iks[1:]

            if len(standoff_iks) == reach_tail_len:
                if not attached:
                    standoff_iks = standoff_iks[::-1]
                reach_traj = np.stack(standoff_iks)
                diff = np.linalg.norm(np.diff(reach_traj, axis=0))

                if diff < 2:  # smooth
                    standoff_ = standoff_iks[0] if not attached else standoff_iks[-1]
                    reach_traj = np.concatenate([reach_traj, finger_joints], axis=-1)
                    reach_goal_set.append(reach_traj)
                    standoff_goal_set.append(
                        np.concatenate([standoff_, finger_joint])
                    )  # [-1]
                    any_ik = True

        else:
            goal_ik = r.inverse_kinematics(
                end_pose[:3], ros_quat(end_pose[3:]), seed=seed
            )
            if goal_ik is not None:
                reach_goal_set.append(np.concatenate([goal_ik, finger_joint]))
                standoff_goal_set.append(np.concatenate([goal_ik, finger_joint]))
                any_ik = True
    return reach_goal_set, standoff_goal_set, any_ik

class Planner(object):
    """
    Planner class that plans a grasp trajectory 
    Tricks such as standoff pregrasp, flip grasps are for real world experiments. 
    """

    def __init__(self, env, traj, lazy=False):
        self.cfg = env.cfg  
        self.env = env
        self.traj = traj
        self.cost = Cost(env)
        self.optim = Optimizer(env, self.cost)
        self.lazy = lazy

        # Planning methods 
        if 'knowngrasps' in self.cfg.method:
            self.load_grasp_set(env)
            self.setup_goal_set(env)
            self.grasp_init(env)
            self.learner = Learner(env, self.traj, self.cost)
        elif 'implicitgrasps' in self.cfg.method:
            if 'fixed' in self.cfg.method:
                self.load_grasp_set(env)
                self.setup_goal_set(env)
                self.grasp_init(env)
            from bullet.methods.implicit import ImplicitGrasp_OutputPose
            self.grasp_predictor = ImplicitGrasp_OutputPose(ckpt_path=self.cfg.grasp_prediction_weights) 
        else:
            raise NotImplementedError

        self.history_trajectories = []
        self.info = []
        self.ik_cache = []

    def grasp_init(self, env=None):
        """
        Use precomputed grasps to initialize the end point and goal set
        """
        grasp_ees = []
        if len(env.objects) > 0:
            self.traj.goal_set = env.objects[env.target_idx].grasps
            self.traj.goal_potentials = env.objects[env.target_idx].grasp_potentials
            if bool(env.objects[env.target_idx].grasps_scores): # not None or empty
                self.traj.goal_quality = env.objects[env.target_idx].grasps_scores
                grasp_ees = env.objects[env.target_idx].grasp_ees
            if self.cfg.use_standoff:
                if len(env.objects[env.target_idx].reach_grasps) > 0:
                    self.traj.goal_set = env.objects[env.target_idx].reach_grasps[:, -1]

        if len(self.traj.goal_set) > 0:
            proj_dist = np.linalg.norm(
                (self.traj.start - np.array(self.traj.goal_set))
                * self.cfg.link_smooth_weight,
                axis=-1,
            )

            if self.traj.goal_quality is None or self.traj.goal_quality == []: # is None or empty
                self.traj.goal_quality = np.ones(len(self.traj.goal_set))

            if self.cfg.goal_idx >= 0: # manual specify
                self.traj.goal_idx = self.cfg.goal_idx

            elif self.cfg.goal_idx == -1:  # initial
                costs = (
                    self.traj.goal_potentials + self.cfg.dist_eps * proj_dist
                )
                self.traj.goal_idx = np.argmin(costs)
 
            else:
                self.traj.goal_idx = 0

            if self.cfg.ol_alg == "Proj":  #
                self.traj.goal_idx = np.argmin(proj_dist)

            if 'outputposegrad' not in self.cfg.method:
                self.traj.end = self.traj.goal_set[self.traj.goal_idx]  #
                self.traj.interpolate_waypoints()

    def flip_grasp(self, old_grasps):
        """
        flip wrist in joint space for augmenting symmetry grasps
        """
        grasps = np.array(old_grasps[:])
        neg_mask, pos_mask = (grasps[..., -3] < 0), (grasps[..., -3] > 0)
        grasps[neg_mask, -3] += np.pi
        grasps[pos_mask, -3] -= np.pi
        limits = (grasps[..., -3] < 2.8973 - self.cfg.soft_joint_limit_padding) * (
            grasps[..., -3] > -2.8973 + self.cfg.soft_joint_limit_padding
        )
        return grasps, limits

    def solve_goal_set_ik(
        self, target_obj, env, pose_grasp, grasp_scores=[], one_trial=False, z_upsample=False, y_upsample=False,
        in_global_coords=False
    ):
        """
        Solve the IKs to the goals
        """

        object_pose = unpack_pose(target_obj.pose)
        start_time = time.time()
        init_seed = self.traj.start[:7]
        reach_tail_len = self.cfg.reach_tail_length
        reach_goal_set = []
        standoff_goal_set = []
        score_set = []
        grasp_set = []
        reach_traj_set = []
        cnt = 0
        anchor_seeds = util_anchor_seeds[: self.cfg.ik_seed_num].copy()

        if one_trial == True:
            seeds = init_seed[None, :]
        else:
            seeds = np.concatenate([init_seed[None, :], anchor_seeds[:, :7]], axis=0)

        """ IK prep """
        if in_global_coords:
            pose_grasp_global = pose_grasp
        else:
            pose_grasp_global = np.matmul(object_pose, pose_grasp)  # gripper -> object

        if z_upsample:
            # Added upright/gravity (support from base for placement) upsampling by object global z rotation
            bin_num = 50
            global_rot_z = np.linspace(-np.pi, np.pi, bin_num)
            global_rot_z = np.stack([rotZ(z_ang) for z_ang in global_rot_z], axis=0)
            translation = object_pose[:3, 3]
            pose_grasp_global[:, :3, 3] = (
                pose_grasp_global[:, :3, 3] - object_pose[:3, 3]
            )  # translate to object origin
            pose_grasp_global = np.matmul(global_rot_z, pose_grasp_global)  # rotate
            pose_grasp_global[:, :3, 3] += translation  # translate back
 
        if y_upsample:
            # Added upsampling by local y rotation around finger antipodal contact 
            bin_num = 10
            global_rot_y = np.linspace(-np.pi / 4, np.pi / 4, bin_num)
            global_rot_y = np.stack([rotY(y_ang) for y_ang in global_rot_y], axis=0)
            finger_translation = pose_grasp_global[:, :3, :3].dot(np.array([0, 0, 0.13])) + pose_grasp_global[:, :3, 3]
            local_rotation = np.matmul(pose_grasp_global[:, :3, :3], global_rot_y[:, None, :3, :3])
            delta_translation  = local_rotation.dot(np.array([0, 0, 0.13]))
            pose_grasp_global = np.tile(pose_grasp_global[:,None], (1, bin_num, 1, 1)) 
            pose_grasp_global[:,:,:3,3]  = (finger_translation[None] - delta_translation).transpose((1,0,2))
            pose_grasp_global[:,:,:3,:3] = local_rotation.transpose((1,0,2,3)) 
            pose_grasp_global = pose_grasp_global.reshape(-1, 4, 4)
 
        # standoff
        pose_standoff = np.tile(np.eye(4), (reach_tail_len, 1, 1, 1))
        if self.cfg.use_standoff:
            pose_standoff[:, 0, 2, 3] = (
                -1
                * self.cfg.standoff_dist
                * np.linspace(0, 1, reach_tail_len, endpoint=False)
            )
        standoff_grasp_global = np.matmul(pose_grasp_global, pose_standoff)
        parallel = self.cfg.ik_parallel
        # parallel = False
        seeds_ = seeds[:]

        if not parallel:
            hand_center = np.empty((0, 3))
            for grasp_idx in range(pose_grasp_global.shape[0]):
                end_pose = pack_pose(pose_grasp_global[grasp_idx])
                if (
                    len(standoff_goal_set) > 0
                    and len(hand_center) > 0
                    and self.cfg.increment_iks
                ):  # augment
                    dists = np.linalg.norm(end_pose[:3] - hand_center, axis=-1)
                    closest_idx, _ = np.argsort(dists)[:5], np.amin(dists)
                    seeds_ = np.concatenate(
                        [
                            seeds,
                            np.array(standoff_goal_set)[closest_idx, :7].reshape(-1, 7),
                        ],
                        axis=0,
                    )

                standoff_pose = standoff_grasp_global[:, grasp_idx]
                reach_goal_set_i, standoff_goal_set_i, any_ik = \
                solve_one_pose_ik(
                    [
                        end_pose,
                        standoff_pose,
                        one_trial,
                        init_seed,
                        target_obj.attached,
                        self.cfg.reach_tail_length,
                        self.cfg.ik_seed_num,
                        self.cfg.use_standoff,
                        seeds_,
                    ]
                )
                reach_goal_set.extend(reach_goal_set_i)
                standoff_goal_set.extend(standoff_goal_set_i)
                if grasp_scores != []:
                    score_set.extend([grasp_scores[grasp_idx] for _ in range(len(standoff_goal_set_i))])
                    grasp_set.extend([pose_grasp_global[grasp_idx] for _ in range(len(standoff_goal_set_i))])

                if not any_ik:
                    cnt += 1
                else:
                    hand_center = np.concatenate(
                        [
                            hand_center,
                            np.tile(end_pose[:3], (len(standoff_goal_set_i), 1)),
                        ],
                        axis=0,
                    )

        else:
            processes = 4 # multiprocessing.cpu_count() // 2
            reach_goal_set = (
                np.zeros([0, self.cfg.reach_tail_length, 9])
                if self.cfg.use_standoff
                else np.zeros([0, 9])
            )
            standoff_goal_set = np.zeros([0, 9])
            grasp_set = np.zeros([0, 7])
            any_ik, cnt = [], 0
            p = multiprocessing.Pool(processes=processes)     
             
            num = pose_grasp_global.shape[0]
            for i in range(0, num, processes):
                param_list = [
                    [
                        pack_pose(pose_grasp_global[idx]),
                        standoff_grasp_global[:, idx],
                        one_trial,
                        init_seed,
                        target_obj.attached,
                        self.cfg.reach_tail_length,
                        self.cfg.ik_seed_num,
                        self.cfg.use_standoff,
                        seeds_,
                    ]
                    for idx in range(i, min(i + processes, num - 1))
                ]

                res = p.map(solve_one_pose_ik, param_list)
                any_ik += [s[2] for s in res]

                if np.sum([s[2] for s in res]) > 0:
                    reach_goal_set = np.concatenate(
                        (
                            reach_goal_set,
                            np.concatenate(
                                [np.array(s[0]) for s in res if len(s[0]) > 0],
                                axis=0,
                            ),
                        ),
                        axis=0,
                    )
                    standoff_goal_set = np.concatenate(
                        (
                            standoff_goal_set,
                            np.concatenate(
                                [s[1] for s in res if len(s[1]) > 0], axis=0
                            ),
                        ),
                        axis=0,
                    )
                    if grasp_scores != []:
                        # grasp score for every new reach goal set result
                        new_score_set = np.concatenate(
                                    [[grasp_scores[i+idx] for _ in range(len(s[1]))]
                                        for idx, s in enumerate(res) if len(s[1]) > 0], axis=0)
                        score_set = np.concatenate(
                            (
                                score_set,
                                new_score_set
                            ),
                            axis=0,
                        )
                        new_grasp_set = np.concatenate(
                                    [[pack_pose(pose_grasp_global[i+idx]) for _ in range(len(s[1]))]
                                        for idx, s in enumerate(res) if len(s[1]) > 0], axis=0)
                        grasp_set = np.concatenate(
                            (
                                grasp_set,
                                new_grasp_set
                            ),
                            axis=0
                        )

                if self.cfg.increment_iks:
                    max_index = np.random.choice(
                        np.arange(len(standoff_goal_set)),
                        min(len(standoff_goal_set), 20),
                    )
                    seeds_ = np.concatenate(
                        (seeds, standoff_goal_set[max_index, :7])
                    )
            p.terminate()
            cnt = np.sum(1 - np.array(any_ik))
        if not self.cfg.silent:
            print(
            "{} IK init time: {:.3f}, failed_ik: {}, goal set num: {}/{}".format(
                target_obj.name,
                time.time() - start_time,
                cnt,
                len(reach_goal_set),
                pose_grasp_global.shape[0],
            )
        )
        return list(reach_goal_set), list(standoff_goal_set), list(score_set), list(grasp_set)

    def load_grasp_set(self, env):
        """
        Example to load precomputed grasps for YCB Objects.
        """
        for i, target_obj in enumerate(env.objects):
            if target_obj.compute_grasp and (i == env.target_idx or not self.lazy):

                if not target_obj.attached:

                    """ simulator generated poses """
                    if len(target_obj.grasps_poses) == 0:
                        """ acronym book poses """
                        if target_obj.name == 'Book_5e90bf1bb411069c115aef9ae267d6b7':
                            from acronym_tools import load_grasps
                            pose_grasp, success = load_grasps(f"/checkpoint/thomasweng/acronym/grasps/Book_5e90bf1bb411069c115aef9ae267d6b7_0.0268818133810836.h5")
                            pose_grasp = pose_grasp[success == 1]

                            if False: 
                                import trimesh
                                from acronym_tools import load_mesh, load_grasps, create_gripper_marker
                                grasp_viz = []
                                for T in pose_grasp[:50]: # visualize unrotated
                                    grasp_viz.append(create_gripper_marker(color=[0, 0, 255]).apply_transform(T).apply_transform(unpack_pose(target_obj.pose)))
                                mesh_root = "/data/manifolds/acronym"
                                grasp_root = "/data/manifolds/acronym/grasps"
                                grasp_path = 'Book_5e90bf1bb411069c115aef9ae267d6b7_0.0268818133810836.h5'
                                obj_mesh = load_mesh(f"{grasp_root}/{grasp_path}", mesh_root_dir=mesh_root)
                                m = obj_mesh.apply_transform(unpack_pose(target_obj.pose))
                                trimesh.Scene([m] + grasp_viz).show()
                        else:
                            simulator_path = (
                                self.cfg.robot_model_path
                                + "/../grasps/simulated/{}.npy".format(target_obj.name)
                            )
                            if not os.path.exists(simulator_path):
                                continue
                            try:
                                simulator_grasp = np.load(simulator_path, allow_pickle=True)
                                pose_grasp = simulator_grasp.item()["transforms"]
                            except:
                                simulator_grasp = np.load(
                                    simulator_path,
                                    allow_pickle=True,
                                    fix_imports=True,
                                    encoding="bytes",
                                )
                                pose_grasp = simulator_grasp.item()[b"transforms"]

                        offset_pose = np.array(rotZ(np.pi / 2)) # rotate about z axis 
                        pose_grasp = np.matmul(pose_grasp, offset_pose)  # flip x, y
                        pose_grasp = ycb_special_case(pose_grasp, target_obj.name)
                        target_obj.grasps_poses = pose_grasp
                    else:
                        pose_grasp = target_obj.grasps_poses
                    z_upsample = False

                else:  # placement
                    pose_grasp = np.linalg.inv(unpack_pose(target_obj.rel_hand_pose))[
                        None
                    ]
                    z_upsample = True

                target_obj.reach_grasps, target_obj.grasps, _, _ = self.solve_goal_set_ik(
                    target_obj, env, pose_grasp, z_upsample=z_upsample, y_upsample=self.cfg.y_upsample
                )
                target_obj.grasp_potentials = []

                if (
                    self.cfg.augment_flip_grasp
                    and not target_obj.attached
                    and len(target_obj.reach_grasps) > 0
                ):
                    """ add augmenting symmetry grasps in C space """
                    flip_grasps, flip_mask = self.flip_grasp(target_obj.grasps)
                    flip_reach, flip_reach_mask = self.flip_grasp(
                        target_obj.reach_grasps
                    )
                    mask = flip_mask
                    target_obj.reach_grasps.extend(list(flip_reach[mask]))
                    target_obj.grasps.extend(list(flip_grasps[mask]))
                target_obj.reach_grasps = np.array(target_obj.reach_grasps)
                target_obj.grasps = np.array(target_obj.grasps)

                if (
                    self.cfg.remove_flip_grasp
                    and len(target_obj.reach_grasps) > 0
                    and not target_obj.attached
                ):
                    """ remove grasps in task space that have large rotation change """
                    start_hand_pose = (
                        self.env.robot.robot_kinematics.forward_kinematics_parallel(
                            wrap_value(self.traj.start)[None]
                        )[0][7]
                    )
                    if self.cfg.use_standoff:
                        n = 5
                        interpolated_traj = multi_interpolate_waypoints(
                            self.traj.start,
                            np.array(target_obj.reach_grasps[:, -1]),
                            n,
                            self.traj.dof, # 9,
                            "linear",
                        )
                        target_hand_pose = (
                            self.env.robot.robot_kinematics.forward_kinematics_parallel(
                                wrap_values(interpolated_traj)
                            )[:, 7]
                        )
                        target_hand_pose = target_hand_pose.reshape(-1, n, 4, 4)
                    else:
                        target_hand_pose = (
                            self.env.robot.robot_kinematics.forward_kinematics_parallel(
                                wrap_values(np.array(target_obj.grasps))
                            )[:, 7]
                        )

                    if len(target_hand_pose.shape) == 3:
                        target_hand_pose = target_hand_pose[:,None]

                    # difference angle
                    R_diff = np.matmul(target_hand_pose[..., :3, :3], start_hand_pose[:3,:3].transpose(1,0))
                    angle = np.abs(np.arccos((np.trace(R_diff, axis1=2, axis2=3) - 1 ) /  2))
                    angle = angle * 180 / np.pi 
                    rot_masks = angle > self.cfg.target_hand_filter_angle
                    z = target_hand_pose[..., :3, 0] / np.linalg.norm(target_hand_pose[..., :3, 0], axis=-1, keepdims=True)
                    downward_masks = z[:,:,-1] < -0.3
                    masks = (rot_masks + downward_masks).sum(-1) > 0
                    target_obj.reach_grasps = list(target_obj.reach_grasps[~masks])
                    target_obj.grasps = list(target_obj.grasps[~masks])

    def setup_goal_set(self, env, filter_collision=True, filter_diversity=True):
        """
        Remove the goals that are in collision
        """
        """ collision """
        for i, target_obj in enumerate(env.objects):
            goal_set = target_obj.grasps
            reach_goal_set = target_obj.reach_grasps

            if False:
                for T_obj2grasp in target_obj.grasps_poses[:30]:
                    import pybullet as p
                    pos, orn = p.getBasePositionAndOrientation(0)
                    T_world2bot = np.eye(4)
                    T_world2bot[:3, :3] = np.asarray(p.getMatrixFromQuaternion(orn)).reshape(3, 3)
                    T_world2bot[:3, 3] = pos
                    draw_pose(T_world2bot)

                    T_bot2obj = np.eye(4)
                    target_q = target_obj.pose[3:] # wxyz
                    R = p.getMatrixFromQuaternion(ros_quat(target_q))
                    T_bot2obj[:3, :3] = np.asarray(R).reshape(3, 3)                    
                    T_bot2obj[:3, 3] = target_obj.pose[:3]
                    draw_pose(T_world2bot @ T_bot2obj)

                    draw_pose(T_world2bot @ T_bot2obj @ T_obj2grasp)                    

            if len(goal_set) > 0 and target_obj.compute_grasp:  # goal_set
                potentials, _, vis_points, collide = self.cost.batch_obstacle_cost(
                    goal_set, special_check_id=i, uncheck_finger_collision=-1
                )  # n x (m + 1) x p (x 3)

                threshold = (
                    0.5
                    * (self.cfg.epsilon - self.cfg.ik_clearance) ** 2
                    / self.cfg.epsilon
                )  #
                collide = collide.sum(-1).sum(-1).detach().cpu().numpy()
                potentials = potentials.sum(dim=(-2, -1)).detach().cpu().numpy()
                ik_goal_num = len(goal_set)

                if filter_collision:
                    collision_free = (
                        collide <= self.cfg.allow_collision_point
                    ).nonzero()  # == 0

                    # new_goal_set = []
                    ik_goal_num = len(goal_set)
                    goal_set = [goal_set[idx] for idx in collision_free[0]]
                    reach_goal_set = [reach_goal_set[idx] for idx in collision_free[0]]
                    if target_obj.grasps_scores is not None and target_obj.grasps_scores != []:
                        try:
                            grasp_scores = [target_obj.grasps_scores[idx] for idx in collision_free[0]]
                            grasp_ees = [target_obj.grasp_ees[idx] for idx in collision_free[0]]
                        except Exception as e:
                            import IPython; IPython.embed()
                    potentials = potentials[collision_free[0]]
                    vis_points = vis_points[collision_free[0]]

                """ diversity """
                diverse = False
                sample = False
                num = len(goal_set)
                indexes = range(num)

                if filter_diversity:
                    if num > 0:
                        diverse = True
                        unique_grasps = [goal_set[0]]  # diversity
                        indexes = []

                        for j, joint in enumerate(goal_set):
                            dists = np.linalg.norm(
                                np.array(unique_grasps) - joint, axis=-1
                            )
                            min_dist = np.amin(dists)
                            if min_dist < 0.5:  # 0.01
                                continue
                            unique_grasps.append(joint)
                            indexes.append(j)
                        num = len(indexes)

                    """ sample """
                if num > 0:
                    sample = True
                    sample_goals = np.random.choice(
                        indexes, min(num, self.cfg.goal_set_max_num), replace=False
                    )

                    target_obj.grasps = [goal_set[int(idx)] for idx in sample_goals]
                    target_obj.reach_grasps = [
                        reach_goal_set[int(idx)] for idx in sample_goals
                    ]
                    if target_obj.grasps_scores is not None and target_obj.grasps_scores != []:
                        target_obj.grasps_scores = [grasp_scores[int(idx)] for idx in sample_goals]
                        target_obj.grasp_ees = [grasp_ees[int(idx)] for idx in sample_goals]
                    target_obj.seeds += target_obj.grasps
                    # compute 5 step interpolation for final reach
                    target_obj.reach_grasps = np.array(target_obj.reach_grasps)
                    target_obj.grasp_potentials.append(potentials[sample_goals])
                    target_obj.grasp_vis_points.append(vis_points[sample_goals])
                    if not self.cfg.silent:
                        print(
                        "{} IK FOUND collision-free goal num {}/{}/{}/{}".format(
                            env.objects[i].name,
                            len(target_obj.reach_grasps),
                            len(target_obj.grasps),
                            num,
                            ik_goal_num,
                            )
                        )
                else:
                    print("{} IK FAIL".format(env.objects[i].name))

                if not sample:
                    target_obj.grasps = []
                    target_obj.reach_grasps = []
                    target_obj.grasps_scores = []
                    target_obj.grasp_ees = []
                    target_obj.grasp_potentials = []
                    target_obj.grasp_vis_points = []
            target_obj.compute_grasp = False

    def get_T_bot2ee(self, traj, idx=-1):
        """
        Returns: numpy matrix
        """
        angles = traj.data[idx]
        end_joints = wrap_value(angles) # rad2deg
        end_poses = self.cfg.ROBOT.forward_kinematics_parallel(
            joint_values=end_joints[np.newaxis, :], base_link=self.cfg.base_link)[0]
        T_bot2ee = end_poses[-3]
        return T_bot2ee

    def get_T_obj2bot(self):
        """
        Returns: numpy matrix
        """
        T_bot2objfrm = pt.transform_from_pq(self.env.objects[self.env.target_idx].pose) 
        T_objfrm2obj = self.cfg.T_obj2ctr
        T_obj2bot = np.linalg.inv(T_bot2objfrm @ T_objfrm2obj)
        return T_obj2bot
    
    def get_T_obj2goal(self, fixed_goal=False):
        """
        Get transform from robot frame to desired grasp frame
        Returns: numpy matrix
        """
        if fixed_goal: # use goal from pre-existing grasp set
            if self.traj.goal_set == []:
                self.load_grasp_set(self.env)
                self.setup_goal_set(self.env)
                self.grasp_init(self.env)

            goal_joints = wrap_value(self.traj.goal_set[self.traj.goal_idx]) # degrees
            goal_poses = self.cfg.ROBOT.forward_kinematics_parallel(
                joint_values=goal_joints[np.newaxis, :], base_link=self.cfg.base_link)[0]
            T_bot2goal = goal_poses[-3]

            if True:
                import pybullet as p
                pos, orn = p.getBasePositionAndOrientation(0)
                T_world2bot = np.eye(4)
                T_world2bot[:3, :3] = np.asarray(p.getMatrixFromQuaternion(orn)).reshape(3, 3)
                T_world2bot[:3, 3] = pos
                draw_pose(T_world2bot @ T_bot2goal)

            T_obj2bot = self.get_T_obj2bot()
            T_obj2goal = T_obj2bot @ T_bot2goal

        else: # predict grasp
            pass

        return T_obj2goal

    def get_joint_angle_grad(self, traj, fixed_goal=False, T_bot2ee_np=None):
        """
        Get joint angle grad from network prediction
        For outputpose grad only
        """
        # Get transform from object to end effector via robot base frame
        T_obj2bot_np = self.get_T_obj2bot()
        T_bot2ee_np = self.get_T_bot2ee(traj)
        T_obj2ee_np = T_obj2bot_np @ T_bot2ee_np 
        T_obj2ee_nograd = torch.tensor(T_obj2ee_np, device='cuda', dtype=torch.float64)

        # Get log map representation of T_ee2obj so we can get gradient for the manipulator Jacobian
        Stheta_ee2obj = pytorch3d.transforms.se3_log_map(torch.linalg.inv(T_obj2ee_nograd).T.unsqueeze(0)).squeeze(0) # [nu omega]
        Stheta_ee2obj.requires_grad = True

        # # Get log map representation of T_obj2ee so we can get the gradient for the manipulator Jacobian
        # Stheta_obj2ee = pytorch3d.transforms.se3_log_map(T_obj2ee_nograd.T.unsqueeze(0)).squeeze(0) # [nu omega], nu = trans, omega = rot
        # Stheta_obj2ee.requires_grad = True

        # Turn Stheta_o2e back into T_obj2ee for backprop gradient flow
        # T_obj2ee = pytorch3d.transforms.se3_exp_map(Stheta_obj2ee.unsqueeze(0)).squeeze().T
        T_obj2ee = torch.linalg.inv(pytorch3d.transforms.se3_exp_map(Stheta_ee2obj.unsqueeze(0)).squeeze().T)
        if not torch.all(torch.isclose(T_obj2ee_nograd, T_obj2ee)):
            print("Warning: transform is not close")
        # assert torch.all(torch.isclose(T_obj2ee_nograd, T_obj2ee))

        # Get transform from object frame to goal grasp
        # Fixed goal (offset from start position)
        T_bot2goal_np = self.get_T_bot2ee(traj, idx=0)
        T_bot2goal_np[:3, 3] += [0.1, 0.1, -0.1]
        T_obj2goal_np = deepcopy(T_obj2bot_np) @ T_bot2goal_np
        # T_obj2goal_np = self.get_T_obj2goal(fixed_goal=fixed_goal)
        T_obj2goal = torch.tensor(T_obj2goal_np, device='cuda', dtype=torch.float64)

        T_ee2goal = torch.linalg.inv(T_obj2ee) @ T_obj2goal
        # Try pose quaternion L2 loss instead of loss in exp coords
        # pq_ee2goal = torch.zeros((7,), device='cuda', dtype=torch.float64)
        # pq_ee2goal[:3] = T_ee2goal[:3, 3]
        # pq_ee2goal[3:] = pytorch3d.transforms.matrix_to_quaternion(T_ee2goal[:3, :3].unsqueeze(0)).squeeze(0) # qw qx qy qz
        # Norm the whole pq vector
        # loss = torch.linalg.norm(pq_ee2goal - torch.tensor([0, 0, 0, 1, 0, 0, 0], device='cuda', dtype=torch.float64))
        
        Stheta_ee2goal = pytorch3d.transforms.se3_log_map(T_ee2goal.T.unsqueeze(0)).squeeze(0) # [nu omega]
        # Compute cost as norm of log map, backprop to get log map velocity
        loss = torch.linalg.norm(Stheta_ee2goal[:3]) + torch.linalg.norm(Stheta_ee2goal[3:])

        loss.backward()
        # Sthetadot_ee = Stheta_obj2ee.grad.cpu().numpy()
        Sthetadot_ee = -Stheta_ee2obj.grad.cpu().numpy()
        # [[3, 4, 5, 0, 1, 2]] # [omega nu]

        # Use adjoint to go from ee frame log map velocity to base frame log map velocity
        # adjoint in pytransform3d is 
        # [  R    0 ]
        # [ [t]_x R ]
        # But for exponential coordinates and our jacobian we need 
        # [ R   [t]_x ]
        # [ 0    R    ]
        adj_ee2bot = pt.adjoint_from_transform(T_bot2ee_np)
        adj_ee2bot[:3, 3:] = adj_ee2bot[3:, :3]
        adj_ee2bot[3:, :3] = np.zeros(3)
        Sthetadot_bot = adj_ee2bot @ Sthetadot_ee # ee is body frame, bot is spatial frame

        # Compute jacobian inverse to get joint angle velocity from log map velocity 
        J = self.cfg.ROBOT.jacobian(traj.end[:7])
        J_pinv = J.T @ np.linalg.inv(J @ J.T)
        q_dot = J_pinv @ Sthetadot_bot

        if self.cfg.use_goal_grad:
            traj.goal_cost = loss.item()
            traj.goal_grad = q_dot
            print(f"cost: {traj.goal_cost}, grad: {traj.goal_grad}")

        if True: # debug viz
            pos, orn = p.getBasePositionAndOrientation(0)
            T_world2bot = np.eye(4)
            T_world2bot[:3, :3] = np.asarray(p.getMatrixFromQuaternion(orn)).reshape(3, 3)
            T_world2bot[:3, 3] = pos
            draw_pose(T_world2bot @ T_bot2ee_np) # ee in world frame
            draw_pose(T_world2bot @ np.linalg.inv(T_obj2bot_np)) # obj in world frame

            draw_pose(T_world2bot @ T_bot2goal_np)

        # if True: # debug viz
            # These should be the same
            # draw_pose(T_world2bot @ np.linalg.inv(T_obj2bot_np) @ T_obj2goal_np)
            # draw_pose(T_world2bot @ T_bot2ee_np @ T_ee2goal.detach().cpu().numpy())

        return T_bot2ee_np

    def pq_from_tau(self, tau):
        pq = torch.zeros((7,), device='cuda', dtype=torch.float64)
        T = ptf.se3_exp_map(tau.unsqueeze(0), eps=1e-10).squeeze().T
        pq[:3] = T[:3, 3]
        pq[3:] = ptf.matrix_to_quaternion(T[:3, :3].unsqueeze(0)).squeeze(0) # qw qx qy qz
        return pq

    def pq_from_T(self, T):
        pq = torch.zeros((7,), device='cuda', dtype=torch.float64)
        pq[:3] = T[:3, 3]
        pq[3:] = ptf.matrix_to_quaternion(T[:3, :3].unsqueeze(0)).squeeze(0) # qw qx qy qz
        return pq

    def compute_loss(self, tau_b2e, tau_b2g, loss_fn='logmap'):
        if loss_fn == 'logmap_split':
            # nu := translational component of the exp. coords
            # omega := rotational component of the exp. coords 
            alpha = 0.01
            nu_diff = tau_b2e[:3] - tau_b2g[:3]
            omega_diff = tau_b2e[3:] - tau_b2g[3:]
            loss = 0.5*torch.linalg.norm(nu_diff)**2 + 0.5*torch.linalg.norm(omega_diff)**2
        elif loss_fn == 'logmap':
            alpha = 0.01
            loss = 0.5*torch.linalg.norm(tau_b2e - tau_b2g)**2
        elif loss_fn == 'pq':
            alpha = 0.05
            pq_b2e = self.pq_from_tau(tau_b2e)
            pq_b2g = self.pq_from_tau(tau_b2g)
            loss = 0.5*torch.linalg.norm(pq_b2e - pq_b2g)**2
        elif loss_fn == 'control_points':
            alpha = 0.02
            T_b2e = ptf.se3_exp_map(tau_b2e.unsqueeze(0), eps=1e-10).squeeze().T 
            T_b2g = ptf.se3_exp_map(tau_b2g.unsqueeze(0), eps=1e-10).squeeze().T 
            cp_b2e = transform_control_points(T_b2e.unsqueeze(0).float(), 1, mode='rt', device='cuda', rotate=True)
            cp_b2g = transform_control_points(T_b2g.unsqueeze(0).float(), 1, mode='rt', device='cuda', rotate=True)
            # loss = control_point_l1_loss(cp_b2e, cp_b2g)
            loss = control_point_l2_loss(cp_b2e, cp_b2g)
            if True:
                T_b2e_np = T_b2e.detach().cpu().numpy()
                for cp in cp_b2e[0]:
                    T = np.eye(4)
                    T[:3, :3] = T_b2e_np[:3, :3]
                    T[:, 3] = cp.detach().cpu().numpy()
                    draw_pose(self.T_world2bot @ T)
        return loss

    def grad_pose_update(self, tau_b2e, tau_b2g, loss_fn='logmap'):
        """
        Update gradient in pose space
        """
        tau_b2e.requires_grad = True

        # Compute loss
        loss = self.compute_loss(tau_b2e, tau_b2g, loss_fn=loss_fn)

        # Backprop the loss and update the query pose
        loss.backward()
        tau_b2e_grad = tau_b2e.grad.detach()

        # Finite difference check
        if True:
            fn = lambda x: (0.5*torch.linalg.norm(x - tau_b2g)**2).unsqueeze(0)
            jac = jacobian(f=fn, initial=tau_b2e)

        # Gradient descent in pose space
        #   Get step size for pose space gradient
        if loss_fn == 'logmap_split' or loss_fn == 'logmap':
            alpha = 0.05
        elif loss_fn == 'pq':
            alpha = 0.05
        elif loss_fn == 'control_points':
            alpha = 0.02
        tau_b2e = (tau_b2e - alpha * tau_b2e_grad).detach()
        if True: # visualize
            T_b2e = ptf.se3_exp_map(tau_b2e.unsqueeze(0)).squeeze().T
            T_b2e_np = T_b2e.detach().cpu().numpy()
            draw_pose(self.T_world2bot @ T_b2e_np, alt_color=True) # ee in world frame
        return tau_b2e

    def grad_joints_update(self, tau_b2e, tau_b2g, q_curr, loss_fn='logmap'):
        """
        update the input transform to move toward goal pose, agnostic to traj opt loop. 
        """
        tau_b2e.requires_grad = True

        # Compute loss
        loss = self.compute_loss(tau_b2e, tau_b2g, loss_fn=loss_fn)
        loss.backward()
        tau_b2e_grad = tau_b2e.grad.detach()

        # Finite difference check
        if True:
            def fn(q):
                '''return loss in exponential coordinates from joint angles'''
                T_b2e_np = self.cfg.ROBOT.forward_kinematics_parallel(
                    joint_values=wrap_value(q.unsqueeze(0).cpu().numpy()), base_link=self.cfg.base_link)[0][-3]
                T_b2e_fd = torch.tensor(T_b2e_np, device='cuda', dtype=torch.float64)
                tau_b2e_fd = ptf.se3_log_map(T_b2e_fd.T.unsqueeze(0), eps=1e-10, cos_bound=1e-10).squeeze(0) # [nu omega]
                return (0.5*torch.linalg.norm(tau_b2e_fd - tau_b2g)**2).unsqueeze(0)
            jac = jacobian(f=fn, initial=torch.tensor(q_curr[0], device='cuda', dtype=torch.float64))

            def fn_e(q):
                '''return exponential coordinates from joint angles'''
                T_b2e_np = self.cfg.ROBOT.forward_kinematics_parallel(
                    joint_values=wrap_value(q.unsqueeze(0).cpu().numpy()), base_link=self.cfg.base_link)[0][-3]
                T_b2e_fd = torch.tensor(T_b2e_np, device='cuda', dtype=torch.float64)
                tau_b2e_fd = ptf.se3_log_map(T_b2e_fd.T.unsqueeze(0), eps=1e-10, cos_bound=1e-10).squeeze(0) # [nu omega]
                return tau_b2e_fd
            jac_e = jacobian(f=fn_e, initial=torch.tensor(q_curr[0], device='cuda', dtype=torch.float64))

        # Gradient descent in joint space using the manipulator Jacobian
        J = self.cfg.ROBOT.jacobian(q_curr.squeeze(0)) # radians
        tau_b2e_np = tau_b2e.detach().unsqueeze(1).cpu().numpy() # 6 x 1
        tau_b2g_np = tau_b2g.detach().unsqueeze(1).cpu().numpy() # 6 x 1
        # tau_b2e_grad = tau_b2e_grad.detach().unsqueeze(1).cpu().numpy() # 6 x 1
        T_b2e_np = self.cfg.ROBOT.forward_kinematics_parallel(
            joint_values=wrap_value(q_curr), base_link=self.cfg.base_link)[0][-3]
        
        # Geometric jacobian
        transforms = self.cfg.ROBOT.forward_kinematics_parallel(
            joint_values=wrap_value(q_curr), base_link=self.cfg.base_link)[0]

        joints_pos = transforms[1:7 + 1, :3, 3]
        ee_pos = transforms[-1, :3, 3]
        axes = transforms[1:7 + 1, :3, 2]
        joints_pos = transforms[:7, :3, 3]
        ee_pos = transforms[3, :3, 3]
        axes = transforms[:7, :3, 2]

        J = np.r_[np.cross(axes, ee_pos - joints_pos).T, axes.T]

        # # Option 1a
        # # With adjoint matrix to transform gradient from body (ee) to spatial (bot) frame
        # #   adjoint matix in pytransform3d is 
        # #   [  R    0 ]
        # #   [ [t]_x R ]
        # #   But for exponential coordinates and our jacobian we need 
        # #   [ R   [t]_x ]
        # #   [ 0    R    ]
        # adj_e2b = pt.adjoint_from_transform(T_b2e_np) # 6 x 6 
        # adj_e2b[:3, 3:] = adj_e2b[3:, :3]
        # adj_e2b[3:, :3] = np.zeros(3)
        # tau_b2e_grad_s = adj_e2b @ tau_b2e_grad # pose gradient in spatial frame

        # # Option 1b
        # # Without adjoint matrix (assume gradient is already in spatial frame?)
        # # tau_b2e_grad_s = tau_b2e_grad # pose gradient in spatial frame

        # # # Use J inv to convert pose gradient joint angle gradient
        # J_pinv = J.T @ np.linalg.inv(J @ J.T) # 7 x 6
        # q_b2e_grad = J_pinv @ tau_b2e_grad_s # 7 x 1 joint angle gradient

        # Option 2
        # Use J from manual backprop calculation instead of J inv 
        # q_b2e_grad = (tau_b2e_np - tau_b2g_np).T @ J # 1 x 7
        q_b2e_grad = (tau_b2e_np - tau_b2g_np).T @ jac_e.cpu().numpy() # 1 x 7

        q_next = deepcopy(q_curr) # 1 x 7
        # q_next = q_curr - 0.1*jac.cpu().numpy()# 1 x 7
        q_next = q_curr - 0.1*q_b2e_grad # 1 x 7

        # q_next = q_curr - 0.01*q_b2e_grad.T # 1 x 7
        # # q_next = q_curr - 0.01*grad_F.T # 1 x 7
        # # q_next = q_curr - 0.01*grad_F_s.T # 1 x 7
        # # # q_next[:, :2] -= 0.01*grad_F[:, :2] 

        T_b2e_np = self.cfg.ROBOT.forward_kinematics_parallel(
            joint_values=wrap_value(q_next), base_link=self.cfg.base_link)[0][-3]
        T_b2e = torch.tensor(T_b2e_np, device='cuda', dtype=torch.float64)
        tau_b2e = ptf.se3_log_map(T_b2e.T.unsqueeze(0), eps=1e-10, cos_bound=1e-10).squeeze(0) # [nu omega]
        draw_pose(self.T_world2bot @ T_b2e_np) # ee in world frame

        # print("joint vs. ee update")
        # print(tau_b2e)
        # print(tau_b2e1)

        return tau_b2e, q_next

    def CHOMP_update(self, traj, tau_b2g, loss='pq'):
        q_curr = traj.data[-1][np.newaxis, :7] 

        # Get current end effector pose in exp coordinates
        T_b2e_np = self.get_T_bot2ee(traj, idx=-1)
        T_b2e = torch.tensor(T_b2e_np, device='cuda', dtype=torch.float64)
        tau_b2e = ptf.se3_log_map(T_b2e.T.unsqueeze(0), eps=1e-10, cos_bound=1e-10).squeeze(0)
        # tau_b2e.requires_grad = True
        draw_pose(self.T_world2bot @ T_b2e_np) # ee in world frame

        # Finite difference gradient with L2 loss on end effector pose
        def fn(q):
            '''return loss in exponential coordinates from joint angles'''
            T_b2e_np = self.cfg.ROBOT.forward_kinematics_parallel(
                joint_values=wrap_value(q.unsqueeze(0).cpu().numpy()), base_link=self.cfg.base_link)[0][-3]
            T_b2e_fd = torch.tensor(T_b2e_np, device='cuda', dtype=torch.float64)
            tau_b2e_fd = ptf.se3_log_map(T_b2e_fd.T.unsqueeze(0), eps=1e-10, cos_bound=1e-10).squeeze(0) # [nu omega]
            pq_b2e_fd = self.pq_from_tau(tau_b2e_fd)
            pq_b2g_fd = self.pq_from_tau(tau_b2g)
            # return (0.5*torch.linalg.norm(tau_b2e_fd - tau_b2g)**2).unsqueeze(0)
            return (0.5*torch.linalg.norm(pq_b2e_fd - pq_b2g_fd)**2).unsqueeze(0)
        jac = jacobian(f=fn, initial=torch.tensor(q_curr[0], device='cuda', dtype=torch.float64))

        # Compute loss
        if loss == 'logmap_split':
            # nu := translational component of the exp. coords
            # omega := rotational component of the exp. coords 
            # alpha = 0.01
            nu_diff = tau_b2e[:3] - tau_b2g[:3]
            omega_diff = tau_b2e[3:] - tau_b2g[3:]
            loss = torch.linalg.norm(nu_diff) + torch.linalg.norm(omega_diff)
        elif loss == 'logmap':
            # alpha = 0.01
            loss = 0.5*torch.linalg.norm(tau_b2e - tau_b2g)**2
        elif loss == 'pq':
            # alpha = 0.05
            pq_b2e = self.pq_from_tau(tau_b2e)
            pq_b2g = self.pq_from_tau(tau_b2g)
            loss = 0.5*torch.linalg.norm(pq_b2e - pq_b2g)**2
        # elif loss == 'control_points':
        #     # alpha = 0.02
        #     T_b2e = ptf.se3_exp_map(tau_b2e.unsqueeze(0), eps=1e-10).squeeze().T 
        #     T_b2g = ptf.se3_exp_map(tau_b2g.unsqueeze(0), eps=1e-10).squeeze().T 
        #     cp_b2e = transform_control_points(T_b2e.unsqueeze(0).float(), 1, mode='rt', device='cuda', rotate=True)
        #     cp_b2g = transform_control_points(T_b2g.unsqueeze(0).float(), 1, mode='rt', device='cuda', rotate=True)
        #     # loss = control_point_l1_loss(cp_b2e, cp_b2g)
        #     loss = control_point_l2_loss(cp_b2e, cp_b2g)
        #     if True:
        #         T_b2e_np = T_b2e.detach().cpu().numpy()
        #         for cp in cp_b2e[0]:
        #             T = np.eye(4)
        #             T[:3, :3] = T_b2e_np[:3, :3]
        #             T[:, 3] = cp.detach().cpu().numpy()
        #             draw_pose(self.T_world2bot @ T)

        # # Backprop the loss and update the query pose
        # loss.backward()
        # tau_b2e_grad = tau_b2e.grad
        # tau_grad = tau_b2e_grad.detach().cpu().numpy() # in spatial frame

        # # Visualize Euclidean gradient
        # tau_b2e = (tau_b2e - 0.01*tau_b2e_grad).detach()
        # T_b2e = ptf.se3_exp_map(tau_b2e.unsqueeze(0), eps=1e-10).squeeze().T
        # draw_pose(self.T_world2bot @ T_b2e.cpu().numpy(), alt_color=True)

        # # Calculate the jacobian expressed in the robot base frame, 
        # # with reference point at the end effector
        # J = self.cfg.ROBOT.jacobian(traj.data[-1][:7]) # radians
        # J_pinv = J.T @ np.linalg.inv(J @ J.T)
        # q_dot = J_pinv @ tau_grad

        traj.goal_cost = loss.item()
        traj.goal_grad = jac.cpu().numpy()
        print(f"cost: {traj.goal_cost}, grad: {traj.goal_grad}")

    def plan(self, traj):
        """
        Run chomp optimizer to do trajectory optmization
        """

        self.history_trajectories = [np.copy(traj.data)]
        self.info = []
        self.selected_goals = []
        start_time_ = time.time()
        alg_switch = self.cfg.ol_alg != "Baseline" and 'implicitgrasps' not in self.cfg.method
        # and self.cfg.ol_alg != "Proj"

        best_traj_idx = -1
        best_traj = None # Save lowest cost trajectory
        best_cost = 1000 
        if (not self.cfg.goal_set_proj) or len(self.traj.goal_set) > 0 \
            or 'implicitgrasps' in self.cfg.method:

            # Get T_world2bot for debug viz
            pos, orn = p.getBasePositionAndOrientation(0)
            self.T_world2bot = np.eye(4)
            self.T_world2bot[:3, :3] = np.asarray(p.getMatrixFromQuaternion(orn)).reshape(3, 3)
            self.T_world2bot[:3, 3] = pos

            # Get fixed goal that the query pose should move towards every iteration
            #   T_b2g := transform from bot to goal pose frame
            #   tau_b2g := exponential coordinates of T_b2g
            # Goal from known good grasp set
            T_o2g = self.get_T_obj2goal(fixed_goal=True)
            T_b2o = np.linalg.inv(self.get_T_obj2bot())
            T_b2g = torch.tensor(T_b2o @ T_o2g, device='cuda', dtype=torch.float64)
            tau_b2g = ptf.se3_log_map(T_b2g.T.unsqueeze(0)).squeeze(0)
            draw_pose(self.T_world2bot @ T_b2o @ T_o2g) # goal in world frame

            # Gradient descent without CHOMP
            # Get a query pose that will update every iteration: object to ee pose
            #   T_b2e := transform from bot to end effector frame
            #   tau_b2e := exponential coordinates of T_b2e, i.e. Log(T_b2e)
            T_b2e_np = self.get_T_bot2ee(traj, idx=-1)
            T_b2e = torch.tensor(T_b2e_np, device='cuda', dtype=torch.float64)
            #   In pose space
            tau_b2e = ptf.se3_log_map(T_b2e.T.unsqueeze(0), eps=1e-10, cos_bound=1e-10).squeeze(0)
            #   In joint space
            q_curr = traj.data[-1][np.newaxis, :7] 
            draw_pose(self.T_world2bot @ T_b2e_np) # ee in world frame

            for t in range(self.cfg.optim_steps + self.cfg.extra_smooth_steps):
                start_time = time.time()

                if (
                    self.cfg.goal_set_proj
                    and alg_switch and t < self.cfg.optim_steps 
                ):
                    self.learner.update_goal()
                    self.selected_goals.append(self.traj.goal_idx)

                if 'implicitgrasps' in self.cfg.method:
                    # T_bot2ee_np = self.get_joint_angle_grad(traj, fixed_goal=True, T_bot2ee_np=T_bot2ee_np)

                    # fixed goal tau_b2g, grad descent in pose space without CHOMP
                    # tau_b2e = self.grad_pose_update(tau_b2e, tau_b2g)

                    # fixed goal tau_b2g, grad descent in joint space without CHOMP
                    # tau_b2e, q_curr = self.grad_joints_update(tau_b2e, tau_b2g, q_curr=q_curr)
                    # traj.goal_cost = 1
                    # traj.goal_grad = np.zeros((7,))

                    # fixed goal tau_b2g, grad descent with CHOMP
                    # self.CHOMP_update(traj, tau_b2g)

                # # compute and store in traj
                # # https://robotics.stackexchange.com/questions/6382/can-a-jacobian-be-used-to-determine-required-joint-angles-for-end-effector-veloc
                # if 'implicitgrasps' in self.cfg.method:
                #     # Get current ee pose
                #     traj.end = traj.data[-1]
                #     end_joints = wrap_value(traj.end) # rad2deg
                #     end_poses = self.cfg.ROBOT.forward_kinematics_parallel(
                #         joint_values=end_joints[np.newaxis, :], base_link=self.cfg.base_link)[0]
                #     T_bot2ee = end_poses[-3]

                #     # Get desired goal pose - Run implicit grasp network that outputs pose                    
                #     T_bot2objfrm = pt.transform_from_pq(self.env.objects[self.env.target_idx].pose) 
                #     T_objfrm2obj = self.cfg.T_obj2ctr

                #     # Fixed goal joints for debugging
                #     if 'fixed' in self.cfg.method:
                #         # goal_joints = wrap_value(np.array([ 0.2118, -0.3085,  0.0597, -2.5309,  0.2207,  2.3577,  0.5107, 0.04  ,  0.04  ]))
                #         goal_joints = wrap_value(self.traj.goal_set[self.traj.goal_idx])
                #         goal_poses = self.cfg.ROBOT.forward_kinematics_parallel(
                #             joint_values=goal_joints[np.newaxis, :], base_link=self.cfg.base_link)[0]
                #         T_bot2goal = goal_poses[-3]
                #         T_obj2goal = np.linalg.inv(T_bot2objfrm @ T_objfrm2obj) @ T_bot2goal
                #         T_obj2goal_wristrot = trajT_to_grasppredT(T_obj2goal)
                #         T_obj2ee = np.linalg.inv(T_bot2objfrm @ T_objfrm2obj) @ T_bot2ee 
                #         T_obj2ee_wristrot = trajT_to_grasppredT(T_obj2ee)
                #     elif self.cfg.method == 'implicitgrasps_outputdistgrad':
                #         T_ee2obj = trajT_to_grasppredT(np.linalg.inv(T_bot2ee) @ (T_bot2objfrm @ T_objfrm2obj))
                #         SE3_ee2obj = SE3.from_matrix(torch.tensor(T_ee2obj, dtype=torch.float32))
                #         Stheta_ee2obj = SE3_ee2obj.log()

                #         batch_x = Stheta_ee2obj.cuda().unsqueeze(0)
                #         batch_x.requires_grad = True
                #         out_dist = self.grasp_predictor.forward(batch_x)
                #     elif self.cfg.method == 'implicitgrasps_outputlmgrad':
                #         T_obj2ee = trajT_to_grasppredT(np.linalg.inv(T_bot2objfrm @ T_objfrm2obj) @ T_bot2ee)
                #         SE3_obj2ee = SE3.from_matrix(torch.tensor(T_obj2ee, dtype=torch.float32))
                #         Stheta_obj2ee = SE3_obj2ee.log() 
                #         batch_x = Stheta_obj2ee.cuda().unsqueeze(0)

                #         # T_ee2obj = trajT_to_grasppredT(np.linalg.inv(T_bot2ee) @ (T_bot2objfrm @ T_objfrm2obj))
                #         # SE3_ee2obj = SE3.from_matrix(torch.tensor(T_ee2obj, dtype=torch.float32))
                #         # Stheta_ee2obj = SE3_ee2obj.log()
                #         # T_eye = torch.eye(4)
                #         # SE3_eye = SE3.from_matrix(T_eye)
                #         # Stheta_eye = SE3_eye.log()
                #         # Stheta_eye.requires_grad = True
                #         # Stheta_ee2obj += Stheta_eye
                #         # batch_x = Stheta_ee2obj.cuda().unsqueeze(0)

                #         batch_x.requires_grad = True
                #         out_lm = self.grasp_predictor.forward(batch_x)
                #         # import IPython; IPython.embed()
                #     elif self.cfg.method == 'implicitgrasps_outputposegrad':
                #         T_obj2ee_np = np.linalg.inv(T_bot2objfrm @ T_objfrm2obj) @ T_bot2ee
                #         T_obj2ee = torch.tensor(T_obj2ee_np, device='cuda', dtype=torch.float64)

                #         # Get log map representation of input with requires grad so we can backprop
                #         Stheta_o2e = pytorch3d.transforms.se3_log_map(T_obj2ee.T.unsqueeze(0)).squeeze(0) # [nu omega]
                #         Stheta_o2e.requires_grad = True

                #         # Have to turn tau_o2e back into T_obj2ee for gradients to flow
                #         T_obj2ee = pytorch3d.transforms.se3_exp_map(Stheta_o2e.unsqueeze(0)).squeeze().T

                #         # pq_obj2ee = torch.tensor(pt.pq_from_transform(T_obj2ee_np), device='cuda', dtype=torch.float64, requires_grad=True) # qw qx qy qz
                        
                #         # # Get transform of obj2ee
                #         # T_obj2ee = torch.eye(4, device='cuda', dtype=torch.float64)
                #         # T_obj2ee[:3, 3] = pq_obj2ee[:3]
                #         # T_obj2ee[:3, :3] = pytorch3d.transforms.quaternion_to_matrix(pq_obj2ee[3:])
                        
                #         # rotate wrist to input to network
                #         T_offset = torch.tensor(rotZ(np.pi / 2), device='cuda', dtype=torch.float64)
                #         T_obj2ee_rot = T_obj2ee @ T_offset
                #         # T_obj2ee_wristrot = trajT_to_grasppredT(T_obj2ee)

                #         pq_obj2ee_rot = torch.zeros((7,), device='cuda', dtype=torch.float64)
                #         pq_obj2ee_rot[:3] = T_obj2ee_rot[:3, 3]
                #         pq_obj2ee_rot[3:] = pytorch3d.transforms.matrix_to_quaternion(T_obj2ee_rot[:3, :3]) # qw qx qy qz
                #         pq_obj2ee_rot[3:] = pq_obj2ee_rot[3:].clone()[[1, 2, 3, 0]] # qx qy qz qw

                #         # pq_obj2ee_wristrot = pt.pq_from_transform(T_obj2ee_wristrot)[[0, 1, 2, 4, 5, 6, 3]] # x y z qx qy qz qw
                #         # pq_obj2ee_wristrot = torch.tensor(pq_obj2ee_wristrot, device='cuda', dtype=torch.float32).unsqueeze(0)                    
                #         pq_obj2goal_rot = self.grasp_predictor.forward(pq_obj2ee_rot.unsqueeze(0)).squeeze(0) # qx qy qz qw
                #         pq_obj2goal_rot[3:] = pq_obj2goal_rot[3:].clone()[[3, 0, 1, 2]] # qw qx qy qz
                #         T_obj2goal_rot = torch.eye(4, device='cuda', dtype=torch.float64)
                #         T_obj2goal_rot[:3, :3] = pytorch3d.transforms.quaternion_to_matrix(pq_obj2goal_rot[3:])
                #         T_obj2goal_rot[:3, 3] = pq_obj2goal_rot[:3]

                #         # unrotate wrist
                #         T_obj2goal = T_obj2goal_rot @ torch.linalg.inv(T_offset)

                #         # pq_obj2goal_wristrot = pq_obj2goal_wristrot.clone()[[0, 1, 2, 6, 3, 4, 5]] # qw qx qy qz
                #         # T_obj2goal_wristrot = pt.transform_from_pq(pq_obj2goal_wristrot.detach().cpu().numpy())
                #         # T_obj2goal = grasppredT_to_trajT(T_obj2goal_wristrot)
                #     else: 
                #         raise NotImplementedError

                #     if not self.cfg.method == 'implicitgrasps_outputdistgrad' and not self.cfg.method == 'implicitgrasps_outputlmgrad':
                #         visualize_predicted_grasp(t, self.cfg, T_obj2ee_rot.detach().cpu().numpy(), T_obj2goal_rot.detach().cpu().numpy(), T_objfrm2obj)
                #     # else:
                #         # visualize_predicted_grad(t, self.cfg, T_obj2ee, out_lm, T_objfrm2obj, show=True)

                #     if self.cfg.use_ik: # IK to get goal joints
                #         # Un-rotate wrist for traj  
                #         T_bot2goal_r = T_bot2objfrm @ T_objfrm2obj @ T_obj2goal 
                #         traj.goal_pose = pt.pq_from_transform(T_bot2goal_r) # x y z qw qx qy qz
                #         init_seed = traj.end[np.newaxis, :7]
                #         goal_joints, _, _ = solve_one_pose_ik(
                #             [
                #                 traj.goal_pose,
                #                 None, # standoff pose
                #                 False, # one trial
                #                 init_seed, # init seed
                #                 False, # target object attached
                #                 self.cfg.reach_tail_length,
                #                 self.cfg.ik_seed_num,
                #                 self.cfg.use_standoff,
                #                 np.concatenate([init_seed, util_anchor_seeds[: self.cfg.ik_seed_num, :7].copy()]),
                #             ]
                #         )
                #         if goal_joints == []:
                #             print("WARNING: no IK solution found, keeping previous goal_joints")
                #         else:
                #             traj.goal_joints = goal_joints[0] # select first of multiple candidates (may be closest to seed)
                #     elif 'grad' in self.cfg.method: # get delta joints using jacobian
                #         if self.cfg.method == 'implicitgrasps_outputdistgrad':
                #             loss = out_dist
                #             loss.backward()
                #             Sthetadot_body = batch_x.grad.squeeze().cpu().numpy()
                #         elif self.cfg.method == 'implicitgrasps_outputlmgrad':
                #             Stheta_ee2goal = out_lm.cpu()
                            
                #             # Use an identity transform to backprop velocity diff
                #             # T_eye = torch.eye(4)
                #             # SE3_eye = SE3.from_matrix(T_eye)
                #             # Stheta_eye = SE3_eye.log()
                #             # Stheta_eye.requires_grad = True
                #             # Stheta_ee2goal += Stheta_eye

                #             # loss = torch.linalg.norm(out_lm)
                #             loss = torch.linalg.norm(Stheta_ee2goal)
                #             loss.backward()
                #             Sthetadot_body = batch_x.grad.squeeze().cpu().numpy()
                #             # Sthetadot_body = -batch_x.grad.squeeze().cpu().numpy()
                #             # Sthetadot_body = -Stheta_eye.grad.numpy()
                #         elif self.cfg.method == 'implicitgrasps_outputposegrad': 
                #             # Get log map transform of end effector to goal
                #             T_ee2goal = torch.linalg.inv(T_obj2ee) @ T_obj2goal
                #             Stheta_e2g = pytorch3d.transforms.se3_log_map(T_ee2goal.T.unsqueeze(0)) # [nu omega]
                            
                #             # SE3_ee2goal = SE3.from_matrix(torch.tensor(T_ee2goal, dtype=torch.float32))
                #             # Stheta_ee2goal = SE3_ee2goal.log()

                #             # # Use an identity transform to backprop velocity diff
                #             # T_eye = torch.eye(4)
                #             # SE3_eye = SE3.from_matrix(T_eye)
                #             # Stheta_eye = SE3_eye.log()
                #             # Stheta_eye.requires_grad = True
                #             # Stheta_ee2goal += Stheta_eye

                #             # Compute cost as norm of log map, backprop to get log map velocity
                #             # loss = torch.linalg.norm(Stheta_ee2goal)
                #             loss = torch.linalg.norm(Stheta_e2g)
                #             loss.backward()
                #             Sthetadot_body = -Stheta_o2e.grad.cpu().numpy()
                #             # Sthetadot_body = -Stheta_eye.grad.numpy()
                #         else:
                #             raise NotImplementedError

                #         # Use adjoint to go from ee frame log map velocity to base frame log map velocity
                #         adjoint = pt.adjoint_from_transform(T_bot2ee)
                #         Sthetadot_spatial = adjoint @ Sthetadot_body

                #         # Compute jacobian inverse to get joint angle velocity from log map velocity 
                #         J = self.cfg.ROBOT.jacobian(traj.end[:7])
                #         J_pinv = J.T @ np.linalg.inv(J @ J.T)
                #         q_dot = J_pinv @ Sthetadot_spatial

                #         # Only used for goal_set_proj
                #         # traj.goal_joints = np.concatenate([traj.end[:7] - 1e-2 * q_dot, [0.04, 0.04]]) 
                #         traj.goal_joints = np.concatenate([traj.end[:7] - 0.1 * q_dot, [0.04, 0.04]]) 

                #         if self.cfg.use_goal_grad:
                #             traj.goal_cost = loss.item()
                #             traj.goal_grad = q_dot
                #             # traj.goal_grad = 0.1*q_dot
                #             print(f"cost: {traj.goal_cost}, grad: {traj.goal_grad}")

                self.info.append(self.optim.optimize(traj, force_update=True))  
                self.history_trajectories.append(np.copy(traj.data))
                if self.cfg.use_min_goal_cost_traj:
                    if traj.goal_cost < best_cost:
                        best_cost = traj.goal_cost
                        best_traj = np.copy(traj.data)
                        best_traj_idx = t

                # TODO save transforms
                # if self.cfg.method == 'implicitgrasps_outputdistgrad' or self.cfg.method == 'implicitgrasps_outputlmgrad':
                    # self.info[-1]["transforms"] = [T_bot2ee, T_bot2objfrm, T_objfrm2obj, None, T_obj2ee]
                # elif 'implicitgrasps' in self.cfg.method:
                    # self.info[-1]["transforms"] = [T_bot2ee, T_bot2objfrm, T_objfrm2obj, T_obj2goal, T_obj2ee]

                if self.cfg.report_time:
                    print("plan optimize:", time.time() - start_time)

                if self.info[-1]["terminate"] and t > 0:
                    break
 
            # compute information for the final
            if not self.info[-1]["terminate"]:
                if self.cfg.use_min_goal_cost_traj:
                    print("Replacing final traj with lowest cost traj")
                    traj.data = best_traj
                    self.info.append(self.optim.optimize(traj, info_only=True))
                    self.history_trajectories.append(best_traj)
                    with open(f'{self.cfg.exp_dir}/{self.cfg.exp_name}/{self.cfg.scene_file}/{best_traj_idx}.txt', 'w') as f:
                        f.write('')
                else:
                    self.info.append(self.optim.optimize(traj, info_only=True))
            else:
                del self.history_trajectories[-1]

            plan_time = time.time() - start_time_
            res = (
                "SUCCESS BE GENTLE"
                if self.info[-1]["terminate"]
                else "FAIL DONT EXECUTE"
            )
            if not self.cfg.silent:
                print(
                "planning time: {:.3f} PLAN {} Length: {}".format(
                    plan_time, res, len(self.history_trajectories[-1])
                )
            )
            self.info[-1]["time"] = plan_time

        else:
            if not self.cfg.silent: print("planning not run...")
        return self.info

# # for traj init end at start

                    # # from copy import deepcopy
                    # # traj.selected_goal = deepcopy(traj.end)
                    # # traj.selected_goal[0] += 1.57 / 2
                    # # traj.goal_joints = traj.selected_goal

                    # # goal_joints = wrap_value(traj.selected_goal)
                    # # end_joints = wrap_value(traj.end) # rad2deg
                    # # end_pose_Ts, goal_pose_Ts = self.cfg.ROBOT.forward_kinematics_parallel(
                    # #     joint_values=np.stack([end_joints, goal_joints]), base_link=self.cfg.base_link)
                    # # traj.end_pose = end_pose_Ts[-3] # end effector 3rd from last
                    # # traj.goal_pose = goal_pose_Ts[-3]
                    # # T_bot2ee = end_pose_Ts[-3]

                    # # Get log map of transform from end effector to grasp goal
                    # # T_bot2goal = goal_pose_Ts[-3]
                    # # T_ee2goal = np.linalg.inv(T_bot2ee) @ T_bot2goal
                    # # T_ee2goal_t = torch.tensor(T_ee2goal, dtype=torch.float32)
                    # # se3_ee2goal = SE3.from_matrix(T_ee2goal_t)
                    # # Stheta_ee2goal = se3_ee2goal.log()
                    # # Stheta_ee2goal += Stheta_eye
                    # T_ee2goal = np.linalg.inv(T_obj2ee) @ T_obj2goal
                    # SE3_ee2goal = SE3.from_matrix(torch.tensor(T_ee2goal, dtype=torch.float32))
                    # Stheta_ee2goal = SE3_ee2goal.log()

                    # # Use an identity transform to backprop velocity diff
                    # T_eye = torch.eye(4)
                    # SE3_eye = SE3.from_matrix(T_eye)
                    # Stheta_eye = SE3_eye.log()
                    # Stheta_eye.requires_grad = True
                    # Stheta_ee2goal += Stheta_eye
                    
                    # # Compute cost as norm of log map, backprop to get log map velocity
                    # loss = torch.linalg.norm(Stheta_ee2goal)
                    # loss.backward()
                    # Sthetadot_body = -Stheta_eye.grad.numpy()

                    # # Use adjoint to go from ee frame log map velocity to base frame log map velocity
                    # adjoint = pt.adjoint_from_transform(T_bot2ee)
                    # Sthetadot_spatial = adjoint @ Sthetadot_body

                    # # Compute jacobian inverse to get joint angle velocity from log map velocity 
                    # J = self.cfg.ROBOT.jacobian(traj.end[:7])
                    # J_pinv = J.T @ np.linalg.inv(J @ J.T)
                    # q_dot = J_pinv @ Sthetadot_spatial

                    # if self.cfg.use_goal_grad:
                    #     traj.goal_cost = loss.item()
                    #     traj.goal_grad = q_dot
                    #     print(f"cost: {traj.goal_cost}, grad: {traj.goal_grad}")





                    # Load grasps (sidestep collision problem? But there will be collision computation in traj opt)
                    # from acronym_tools import load_grasps
                    # rotgrasp2grasp_T = pt.transform_from(pr.matrix_from_axis_angle([0, 0, 1, -np.pi/2]), [0, 0, 0])
                    # obj2rotgrasp_Ts, success = load_grasps(f"/data/manifolds/acronym/grasps/Book_5e90bf1bb411069c115aef9ae267d6b7_0.0268818133810836.h5")
                    # obj2grasp_Ts = (obj2rotgrasp_Ts @ rotgrasp2grasp_T)[success == 1]

                    # from utils import *
                    # T_bot2objfrm = pt.transform_from_pq(self.env.objects[0].pose) 
                    # T_objfrm2obj = self.cfg.T_obj2ctr
                    # T_bot2obj = T_bot2objfrm @ T_objfrm2obj
                    # grasps = T_bot2obj @ obj2grasp_Ts #[:10] # limit number

                    # Use ee2obj to transform pc    
                    # if pc is not None:
                    #     pc_obj = pc
                    #     se3_ee2obj = SE3.exp(Stheta_ee2obj)
                    #     pc_obj = torch.tensor(pc_obj, dtype=torch.float32)
                    #     pc_ee = (se3_ee2obj.as_matrix() @ pc_obj.T).T
                        
                    #     # Compute loss and get grad from backprop
                    #     # TODO use implicit model
                    #     loss = torch.linalg.norm(Stheta_ee2obj)
                    # else:
                    # Compute loss and get grad from backprop

                    # l1 cost function
                    # gt_T = torch.tensor(traj.goal_pose[np.newaxis, ...], dtype=torch.float32)
                    # query_T = torch.tensor(traj.end_pose[np.newaxis, ...], dtype=torch.float32, requires_grad=True)
                    # loss = torch.nn.functional.l1_loss(query_T, gt_T)
                    # loss.backward()
                    # dloss_dg = query_T.grad # g is query as SE(3)
                    # goal_grad = query_T.grad.sum()
                    # goal_cost = goal_cost.item()
                    # goal_grad = goal_grad.item()

                    # control points cost function
                    # goal_cost, goal_grad = get_control_pts_goal_cost(traj.goal_pose, traj.end_pose)

                    # import pybullet as p
                    # pos, orn = p.getBasePositionAndOrientation(0)
                    # mat = np.asarray(p.getMatrixFromQuaternion(orn)).reshape(3, 3)
                    # T_world2bot = np.eye(4)
                    # T_world2bot[:3, :3] = mat
                    # T_world2bot[:3, 3] = pos



    # def load_grasp_set_gn(self, env, grasps, grasp_scores):
    #     """
    #     Load grasps from graspnet as grasp set.
    #     """
    #     for i, target_obj in enumerate(env.objects):
    #         if target_obj.compute_grasp and (i == env.target_idx or not self.lazy):
    #             if not target_obj.attached:
    #                 # offset_pose = np.array(rotZ(np.pi / 2))  # and
    #                 # target_obj.grasps_poses = np.matmul(grasps, offset_pose)  # flip x, y # TODO not sure if this is still necessary
    #                 target_obj.grasps_poses = grasps # acronym_book
    #                 target_obj.grasps_scores = grasp_scores
    #                 z_upsample = False
    #             else:
    #                 print("Target attached")
    #                 import IPython; IPython.embed()
    #                 z_upsample=True

    #             # import trimesh
    #             # from acronym_tools import load_mesh, load_grasps, create_gripper_marker
    #             # inf_viz = []
    #             # # for T in target_obj.grasps_poses:
    #             #     # inf_viz.append(create_gripper_marker(color=[0, 0, 255]).apply_transform(T))
    #             # for T in grasps: # visualize unrotated
    #             #     inf_viz.append(create_gripper_marker(color=[0, 0, 255]).apply_transform(T))
    #             # mesh_root = "/data/manifolds/acronym"
    #             # grasp_root = "/data/manifolds/acronym/grasps"
    #             # grasp_path = 'Book_5e90bf1bb411069c115aef9ae267d6b7_0.0268818133810836.h5'
    #             # obj_mesh, obj_scale = load_mesh(f"{grasp_root}/{grasp_path}", mesh_root_dir=mesh_root, ret_scale=True)
    #             # m = obj_mesh.apply_transform(unpack_pose(target_obj.pose))
    #             # trimesh.Scene([m] + inf_viz).show()

    #             target_obj.reach_grasps, target_obj.grasps, target_obj.grasps_scores, target_obj.grasp_ees = self.solve_goal_set_ik(
    #                 target_obj, env, target_obj.grasps_poses, grasp_scores=target_obj.grasps_scores, z_upsample=z_upsample, y_upsample=self.cfg.y_upsample,
    #                 in_global_coords=True
    #             )
    #             target_obj.grasp_potentials = []

    # # update planner according to the env
    # def update(self, env, traj):
    #     self.cfg = config.cfg
    #     self.env = env
    #     self.traj = traj
    #     # update cost
    #     self.cost.env = env
    #     self.cost.cfg = config.cfg
    #     if len(self.env.objects) > 0:
    #         self.cost.target_obj = self.env.objects[self.env.target_idx]

    #     # update optimizer
    #     self.optim = Optimizer(env, self.cost)

    #     # load grasps if needed
    #     if self.grasps is not None:
    #         self.load_grasp_set_gn(env, self.grasps, self.grasp_scores)
    #         self.setup_goal_set(env)
    #         self.grasp_init(env)
    #     else:
    #         if self.cfg.goal_set_proj:
    #             if self.cfg.scene_file == "" or self.cfg.traj_init == "grasp":
    #                 self.load_grasp_set(env)
    #                 self.setup_goal_set(env)
    #             else:
    #                 self.load_goal_from_scene()

    #             self.grasp_init(env)
    #             self.learner = Learner(env, traj, self.cost)
    #         else:
    #             self.traj.interpolate_waypoints()
    #     self.history_trajectories = []
    #     self.info = []
    #     self.ik_cache = []

    # def load_goal_from_scene(self):
    #     """
    #     Load saved goals from scene file, standoff is not used.
    #     """
    #     file = self.cfg.scene_path + self.cfg.scene_file + ".mat"
    #     if self.cfg.traj_init == "scene":
    #         self.cfg.use_standoff = False
    #     if os.path.exists(file):
    #         scene = sio.loadmat(file)
    #         self.cfg.goal_set_max_num = len(scene["goals"])
    #         indexes = range(self.cfg.goal_set_max_num)
    #         self.traj.goal_set = scene["goals"][indexes]
    #         if "grasp_qualities" in scene:
    #             self.traj.goal_quality = scene["grasp_qualities"][0][indexes]
    #             self.traj.goal_potentials = scene["grasp_potentials"][0][indexes]
    #         else:
    #             self.traj.goal_quality = np.zeros(self.cfg.goal_set_max_num)
    #             self.traj.goal_potentials = np.zeros(self.cfg.goal_set_max_num)