LabelPaw 岩芯裂隙标注指南

== 启动 ==
Windows 先启动 VcXsrv（托盘显示企鹅图标），然后在 WSL 终端执行：

ssh -X -o ForwardX11=yes wangzy@10.102.33.137 'cd /home/wangzy/Project/SAM3_gxr/LabelPaw && source ~/anaconda3/bin/activate sam3_gxr && python main.py'

窗口不关。

== 标准工作流 ==

1. 打开图片文件夹：工具栏 打开目录 选择图片目录
2. 添加类别：右侧面板 + 按钮，输入类别名（如 crack、intrusion、filling）
3. 加载 SAM3 模型：顶部工具栏选 SAM3 -> 加载
4. SAM3 预标注：
   - 开启智能辅助（快捷键 Q）
   - 右下角输入提示词（如 thin dark line，空格为短语分隔）
   - 回车提交，SAM3 自动检出
5. 人工修正：
   - 漏检：矩形(R)或 多边形(P) 手动框选
   - 误检：选中 Delete
   - 类别修改：选中 E 键
6. 切换图片：A / 左键（上一张）D / 右键（下一张）
7. 保存：Ctrl+S

== 置信度调节 ==
右侧面板全局置信度滑块：控制 SAM3 检出阈值，越低检出越多但误检可能增加。
单个类别可独立设置信度：右键标签 -> 设置标签置信度。

== 标签管理 ==
右键标签 -> 删除类别（正在使用的标签无法删除）。
右键标签 -> 修改标签颜色。

== 快捷键 ==
Q      SAM 智能辅助开关
R      矩形标注
P      多边形标注
Ctrl+Z 撤销
Ctrl+S 保存
A/左   上一张
D/右   下一张
E      修改选中标注的类别
Del    删除选中标注
