
[basic]
type = axmodel
model_npu = model_9974_npu.axmodel
model_vnpu = model_9974_vnpu.axmodel

[extra]
model_type = yolo26
type=detector
input_type = rgb

input_cache = true
output_cache = true
input_cache_flush = false
output_cache_inval = true

labels = cone, cube, pyramid, 5edge, 6edge, 3edge, 0edge
mean = 0, 0, 0
scale = 0.00392156862745098, 0.00392156862745098, 0.00392156862745098

