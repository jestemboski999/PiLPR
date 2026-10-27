#!/bin/bash
cd /home/admin/hailo-apps
source setup_env.sh

python3 collect_plates.py \
  --input /dev/video0 \
  --frame-rate 30 \
  --hef-path hailo_yolov8n_4_classes_vga \
  --labels-json /home/admin/hailo-apps/local_resources/hailo_4_classes_lp.json \
  --remove-labels /home/admin/hailo-apps/local_resources/labels/remove_labels_license_plate.txt \
  --track-class-id 4 \
  --horizontal-mirror --vertical-mirror
