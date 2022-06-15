import os
import random
import pybullet as p
import numpy as np
from omg.config import cfg
from omg.core import PlanningScene

from omg_bullet.panda_env import PandaEnv
from omg_bullet.panda_ycb_env import PandaYCBEnv

from omg_bullet.utils import bullet_execute_plan

import torch
import pytransform3d.rotations as pr
# import pytransform3d.transformations as pt
from pathlib import Path
import csv
import cv2
import subprocess
import yaml
import hydra
from omegaconf import OmegaConf
from omegaconf.listconfig import ListConfig


def merge_cfgs(hydra_cfg, cfg):
    """Two different configs are being used, the config from omg, and hydra
    Hydra handles command line overrides and also redirects the working directory
    Override cfg with hydra cfgs

    Args:
        hydra_cfg (_type_): _description_
        cfg (_type_): _description_
    """
    for key in hydra_cfg.eval.keys():
        if key in cfg.keys():
            val = hydra_cfg.eval[key]
            cfg[key] = val if type(val) != ListConfig else list(val)
    for key in hydra_cfg.variant.keys():
        if key in cfg.keys():
            val = hydra_cfg.variant[key]
            cfg[key] = val if type(val) != ListConfig else list(val)
    for key in hydra_cfg.keys():
        if key in cfg.keys():
            val = hydra_cfg[key]
            cfg[key] = val if type(val) != ListConfig else list(val)
    cfg.get_global_param()


def init_video_writer(path, obj_name, scene_idx):
    return cv2.VideoWriter(
        f"{path}/{obj_name}_{scene_idx}.avi",
        cv2.VideoWriter_fourcc(*"MJPG"),
        10.0,
        (640, 480),
    )


def init_dir(hydra_cfg):
    cwd = Path(os.getcwd())
    (cwd / 'info').mkdir() 
    (cwd / 'videos').mkdir() 
    (cwd / 'gifs').mkdir() 
    with open(cwd / 'hydra_config.yaml', 'w') as yaml_file:
        OmegaConf.save(config=hydra_cfg, f=yaml_file.name)
    with open(cwd / 'config.yaml', 'w') as yaml_file:
        save_cfg = cfg.copy()
        save_cfg['ROBOT'] = None
        yaml.dump(save_cfg, yaml_file)
    with open(cwd / 'metrics.csv', 'w', newline='') as csvfile:
        fieldnames = ['object_name', 'scene_idx', 'execution', 'planning', 'smoothness', 'collision', 'time']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()


def save_metrics(objname, scene_idx, grasp_success, info):
    has_plan = info != []
    metrics = {
        'object_name': objname,
        'scene_idx': scene_idx,
        'execution': grasp_success,
        'planning': info[-1]['execute'] if has_plan else np.nan,
        'smoothness': info[-1]['smooth'] if has_plan else np.nan,
        'collision': info[-1]['obs'] if has_plan else np.nan,
        'time': info[-1]['time'] if has_plan else np.nan,
    }
    cwd = Path(os.getcwd())
    with open(cwd / 'metrics.csv', 'a', newline='') as csvfile:
        fieldnames = ['object_name', 'scene_idx', 'execution', 'planning', 'smoothness', 'collision', 'time']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writerow(metrics) 


def init_env(hydra_cfg):
    if cfg.eval_env == 'panda_env':
        env = PandaEnv(renders=hydra_cfg.render, gravity=cfg.gravity, cam_look=cfg.cam_look)
    elif cfg.eval_env == 'panda_ycb_env':
        env = PandaYCBEnv(gravity=cfg.gravity)
    else:
        raise NotImplementedError
    return env

@hydra.main(config_path=str(Path(os.path.dirname(__file__)) / '..' / 'config'), 
            config_name="panda_scene", version_base=None)
def main(hydra_cfg):
    np.random.seed(0)
    random.seed(0)
    torch.manual_seed(0)

    merge_cfgs(hydra_cfg, cfg)
    init_dir(hydra_cfg)
    env = init_env(hydra_cfg)

    # Change this so that scenes contains all object and scene permutations
    # Then just run all the scenes. 
    # This will streamline panda_env vs. panda_ycb_env as well. 

    scenes = env.get_scenes(hydra_cfg)
    for scene in scenes:
        planning_scene = PlanningScene(cfg)
        
        obs, objname, scene_name = env.init_scene(scene, planning_scene, hydra_cfg)

        # if cfg.eval_env == 'panda_env':
        #     objinfo = get_object_info(env, objname, Path(hydra_cfg.data_root) / hydra_cfg.dataset)
        #     env.reset(init_joints=scenes[scene_idx]['joints'], no_table=not cfg.table, objinfo=objinfo)
        #     place_object(env, cfg.tgt_pos, q=scenes[scene_idx]['obj_rot'], random=False, gravity=cfg.gravity)
        #     obs = env._get_observation(get_pc=cfg.pc, single_view=False)
        #     set_scene_env(scene, env._objectUids[0], objinfo, scenes[scene_idx]['joints'], hydra_cfg)
        # elif cfg.eval_env == 'panda_ycb_env':
        #     full_name = Path(hydra_cfg.data_root) / 'data' / 'scenes' / f'{scenes[scene_idx]}.mat'
        #     env.cache_reset(scene_file=full_name)
        #     obj_names, obj_poses = env.get_env_info()
        #     object_lists = [name.split("/")[-1].strip() for name in obj_names]
        #     object_poses = [pack_pose(pose) for pose in obj_poses]

        #     exists_ids, placed_poses = [], []
        #     for i, name in enumerate(object_lists[:-2]):  # update planning scene
        #         scene.env.update_pose(name, object_poses[i])
        #         obj_idx = env.obj_path[:-2].index("data/objects/" + name)
        #         exists_ids.append(obj_idx)
        #         trans, orn = env.cache_object_poses[obj_idx]
        #         placed_poses.append(np.hstack([trans, ros_quat(orn)]))
            
        #     cfg.disable_collision_set = [
        #         name.split("/")[-2]
        #         for obj_idx, name in enumerate(env.obj_path[:-2])
        #         if obj_idx not in exists_ids
        #     ]
        #     scene.env.set_target(env.obj_path[env.target_idx].split("/")[-1])
        #     scene.reset(lazy=True)
                
        pc = obs['points'] if cfg.pc else None
        info = planning_scene.step(pc=pc, viz_env=env)
        plan = planning_scene.planner.history_trajectories[-1]

        video_writer = init_video_writer(Path(os.getcwd()) / 'videos', objname, scene_name) if hydra_cfg.write_video else None
        grasp_success = bullet_execute_plan(env, plan, hydra_cfg.write_video, video_writer)

        save_metrics(objname, scene_name, grasp_success, info)
        cwd = Path(os.getcwd())
        np.savez(cwd / 'info' / f'{objname}_{scene_name}', info=info, trajs=planning_scene.planner.history_trajectories)

        # Convert avi to high quality gif 
        if hydra_cfg.write_video and info != []:
            subprocess.Popen(['ffmpeg', '-y', '-i', cwd / 'videos' / f'{objname}_{scene_name}.avi', '-vf', "fps=10,scale=320:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse", '-loop', '0', cwd / 'gifs' / f'{objname}_{scene_name}.gif'])

if __name__ == '__main__':
    main()
