#!/bin/bash
# conda activate gmanifolds
# cd ~/projects/manifolds/OMG-Planner

# bash bash_scripts/run.sh dbg 1

EXP_NAME="$1"
N_TRIALS="$2"

# shape code min error
for ((i=0;i<$2;i++)) do
    python -m bullet.panda_scene \
        --method=GF_learned_singleshape_minerr --write_video \
        --no-render \
        --eval_type=1obj_float_fixedpose_nograv \
        --smoothness_base_weight=0.1 \
        --base_obstacle_weight=0.7 \
        --base_grasp_weight=12 \
        --base_step_size=0.2 \
        --optim_steps=250 \
        --goal_thresh=0.01 \
        --dset_root='/data/manifolds/acronym_mini_relabel' \
        --ckpt='/data/manifolds/fb_runs/multirun/pq-pq_relabel_shape/2022-05-15_204054/lossl1_lr0.0001/default_default/8_8/checkpoints/epoch=479-step=161749.ckpt' \
        -o /data/manifolds/pybullet_eval/$EXP_NAME
done

# shape code min loss
for ((i=0;i<$2;i++)) do
    python -m bullet.panda_scene \
        --method=GF_learned_singleshape_minloss --write_video \
        --no-render \
        --eval_type=1obj_float_fixedpose_nograv \
        --smoothness_base_weight=0.1 \
        --base_obstacle_weight=0.7 \
        --base_grasp_weight=12 \
        --base_step_size=0.2 \
        --optim_steps=250 \
        --goal_thresh=0.01 \
        --dset_root='/data/manifolds/acronym_mini_relabel' \
        --ckpt='/data/manifolds/fb_runs/multirun/pq-pq_relabel_shape/2022-05-15_204054/lossl1_lr0.0001/default_default/8_8/checkpoints/epoch=429-step=144749.ckpt' \
        -o /data/manifolds/pybullet_eval/$EXP_NAME
done

        # --pc \
        # --render \
        # --no-render \

# shape code min loss
# for ((i=0;i<$2;i++)) do
#     python -m bullet.panda_scene \
#         --method=GF_learned_shape_minloss --write_video \
#         --render \
#         --eval_type=1obj_float_fixedpose_nograv \
#         --smoothness_base_weight=0.1 \
#         --base_obstacle_weight=0.7 \
#         --base_grasp_weight=12 \
#         --base_step_size=0.2 \
#         --optim_steps=250 \
#         --goal_thresh=0.01 \
#         --dset_root='/data/manifolds/acronym_mini_relabel' \
#         --pc \
#         --ckpt='/data/manifolds/fb_runs/multirun/pq-pq_relabel_shape100fps/2022-05-22_143051/0/default_default/51_51/checkpoints/epoch=253-step=306425.ckpt' \
#         -o /data/manifolds/pybullet_eval/$EXP_NAME
# done
        # --no-render \
        # --prefix=sm{smooth_weight}_ob{obstacle_weight}_gr{grasp_weight}_st{step_size}_os{optim_steps}_th{goal_thresh}_ \

# # shape code 
# python -m bullet.panda_scene \
# 	--method=GF_learned --write_video --render \
# 	--smoothness_base_weight=0.1 --base_obstacle_weight=0.7 --base_step_size=0.2 \
# 	--base_grasp_weight=10.0 \
# 	--optim_steps=250 --goal_thresh=0.01 \
# 	--prefix=sm{smooth_weight}_ob{obstacle_weight}_gr{grasp_weight}_st{step_size}_os{optim_steps}_th{goal_thresh}_ \
# 	-o /data/manifolds/pybullet_eval/dbg --pc \
# 	--ckpt /data/manifolds/fb_runs/multirun/pq-pq_relabel_shape100fps/2022-05-21_224607/0/hpc_ckpt_44.ckpt
# python -m bullet.panda_scene \
# 	--method=GF_learned --write_video --render \
# 	--smoothness_base_weight=0.1 --base_obstacle_weight=0.7 --base_step_size=0.2 \
# 	--base_grasp_weight=10.0 \
# 	--optim_steps=250 --goal_thresh=0.01 \
# 	--prefix=sm{smooth_weight}_ob{obstacle_weight}_gr{grasp_weight}_st{step_size}_os{optim_steps}_th{goal_thresh}_ \
# 	-o /data/manifolds/pybullet_eval/dbg

# # shape code 
# python -m bullet.panda_scene \
# 	--method=GF_learned --write_video --render \
# 	--smoothness_base_weight=0.1 --base_obstacle_weight=0.7 --base_step_size=0.2 \
# 	--base_grasp_weight=10.0 \
# 	--optim_steps=250 --goal_thresh=0.01 \
# 	--prefix=sm{smooth_weight}_ob{obstacle_weight}_gr{grasp_weight}_st{step_size}_os{optim_steps}_th{goal_thresh}_ \
# 	-o /data/manifolds/pybullet_eval/dbg --pc \
# 	--ckpt /data/manifolds/fb_runs/multirun/pq-pq_relabel_shape100fps/2022-05-21_224607/0/hpc_ckpt_44.ckpt

# for ((i=0;i<$2;i++)) do
#     python -m bullet.panda_scene --method=GF_learned \
#         --write_video \
#         --no-render \
#         --smoothness_base_weight=0.1 \
#         --base_obstacle_weight=0.7 \
#         --base_grasp_weight=12 \
#         --base_step_size=0.2 \
#         --optim_steps=250 \
#         --goal_thresh=0.01 \
#         --ckpt='/data/manifolds/fb_runs/multirun/pq-pq_mini_relabel/2022-05-16_175103/lossl1_lr0.0001/default_default/2_2/checkpoints/epoch=259-step=88211.ckpt' \
#         --dset_root='/data/manifolds/acronym_mini_relabel' \
#         --prefix='minloss_sm0.1_ob0.7_gr12_st0.2_os250_th0.01_' \
#         -o /data/manifolds/pybullet_eval/$EXP_NAME
# done

# for ((i=0;i<$2;i++)) do
#     python -m bullet.panda_scene --method=GF_learned \
#         --write_video \
#         --no-render \
#         --smoothness_base_weight=0.1 \
#         --base_obstacle_weight=0.7 \
#         --base_grasp_weight=12 \
#         --base_step_size=0.2 \
#         --optim_steps=250 \
#         --goal_thresh=0.01 \
#         --ckpt='/data/manifolds/fb_runs/multirun/pq-pq_relabel_shape/2022-05-15_204054/lossl1_lr0.0001/default_default/8_8/checkpoints/epoch=479-step=161749.ckpt' \
#         --dset_root='/data/manifolds/acronym_mini_relabel' \
#         --prefix='shapeminerr_sm0.1_ob0.7_gr12_st0.2_os250_th0.01_' \
#         -o /data/manifolds/pybullet_eval/$EXP_NAME
# done
