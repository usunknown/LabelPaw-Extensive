import sys
import os
import json
from PySide6.QtWidgets import QApplication, QMainWindow, QFileDialog, QInputDialog, QMessageBox, QLabel, QListWidgetItem, QDialog, QMenu, QAbstractItemView
from PySide6.QtCore import Qt, QPointF, QRectF
from PySide6.QtGui import QPainter, QIcon, QPixmap, QColor, QAction, QActionGroup, QPolygonF, QMovie
from main_dataset_tool import DatasetToolWindow
try:
    from ui.author_info import AuthorInfoDialog
except ImportError:
    pass
from ui.main_window import Ui_MainWindow, TemplateSelectorWidget, FormatSelectorWidget
from ui.template_dialog import SkeletonTemplateDialog
from ui.model_selector_dialog import ModelSelectorDialog
from ui.theme import DARK_THEME, LIGHT_THEME
from core.canvas import Canvas, CanvasMode
from core.sam_client import SAMClient, SAM_MODEL_MAP
from core.exporter import Exporter
from core.shapes import RectShape, PolyShape, PointShape, RotatedRectShape, PoseShape
from core.pose_template import TemplateManager
from utils.message import DialogOver
from core.yolo_predictor import YoloPredictorWorker


class MainWindow(QMainWindow, Ui_MainWindow):
    def __init__(self):
        super().__init__()
        self.setupUi(self)

        self.template_manager = TemplateManager()

        self.is_dark_theme = False
        self.setStyleSheet(LIGHT_THEME)

        self.scene = Canvas(self)
        self.view.setScene(self.scene)

        self.current_image_path = None
        self.current_dir = None
        self.class_list = []
        self.current_format = "json"

        self.modeLabel = QLabel("模式: 矩形标注")
        self.statusBar.addWidget(self.modeLabel)

        self.helpLabel = QLabel("状态: 正在初始化")
        self.statusBar.addWidget(self.helpLabel)

        self.sam_client = SAMClient(self)
        self.sam_client.inference_result.connect(self.scene.handle_sam_result)
        self.sam_client.text_result_ready.connect(self.handle_text_results)
        self.sam_client.model_status_changed.connect(self.update_model_status)
        self.scene.sam_client = self.sam_client

        # 撤销/重做时数据栈
        self.undo_stack = []
        self.redo_stack = []
        self.max_history_steps = 20  # 保留20步历史记录
        self.scene.state_changed.connect(self.push_state)  # 绑定画板信号
        
        self._init_pose_templates()

        self._connect_signals()
        self._set_mode(CanvasMode.RECT)
        
        # 初始化模型下拉菜单
        self._init_model_selector()
        
        # 默认加载 SAM 3
        self.on_model_selected("sam3")
        
        self.yolo_worker = None
        
        self.btnPredict.setIcon(QIcon("ui/icon/lightning-fill.svg"))
        
        # 预测按钮动画
        self.predict_movie = QMovie("ui/icon/Loading.gif")
        self.predict_movie.frameChanged.connect(self.update_predict_icon)

    def update_predict_icon(self):
        self.btnPredict.setIcon(QIcon(self.predict_movie.currentPixmap()))

    def _init_pose_templates(self):
        templates = self.template_manager.get_template_names()
        self.templateWidget.update_templates(templates, main_window=self)
        if not self.scene.current_pose_template:
            self.scene.current_pose_template = self.template_manager.get_template("Person (COCO)")

    def _init_model_selector(self):
        self.btnModelSelector.clicked.connect(self._show_model_selector)

    def _show_model_selector(self):
        dialog = ModelSelectorDialog(current_model_key=getattr(self.sam_client, 'current_model_key', None), is_dark_theme=self.is_dark_theme, parent=self)
        dialog.model_selected.connect(self.on_model_selected)
        
        # Position popup near button
        pos = self.btnModelSelector.mapToGlobal(self.btnModelSelector.rect().bottomLeft())
        dialog.move(pos.x(), pos.y() + 5)
        dialog.exec()

    def on_model_selected(self, model_info_or_key):
        if isinstance(model_info_or_key, str):
            key = model_info_or_key
            if key in SAM_MODEL_MAP:
                model_info = SAM_MODEL_MAP[key]
            else:
                model_info = {"key": key, "display_name": key, "type": "sam2", "supports_text": False}
        else:
            model_info = model_info_or_key
            key = model_info["key"]
            
        display_name = model_info.get("display_name", key)
        
        self.btnModelSelector.setText(f" {display_name} ▾")
        self.btnModelSelector.setIcon(QIcon("ui/icon/s.svg"))
        
        if key in SAM_MODEL_MAP or model_info.get("type", "").startswith("sam"):
            self.btnPredict.hide()
            self.current_yolo_predictor = None
            if key not in SAM_MODEL_MAP:
                SAM_MODEL_MAP[key] = model_info
            self.sam_client.load_model_by_key(key)
            # 如果当前在点标注模式（不支持SAM），自动切换回矩形标注
            if self.scene.mode == CanvasMode.POINT:
                self.actionRect.trigger()
        elif model_info.get("type", "").startswith("yolo"):
            self.sam_client.cleanup()
            self.sam_client.current_model_key = key
            from core.yolo_predictor import YoloPredictor
            path = model_info.get("path", "")
            if path and os.path.exists(path):
                try:
                    self.current_yolo_predictor = YoloPredictor(path)
                    
                    # 自动根据 YOLO 任务类型切换绘制模式
                    task = getattr(self.current_yolo_predictor, 'task', 'detect')
                    if task == 'detect':
                        self.actionRect.trigger()
                    elif task == 'segment':
                        self.actionPoly.trigger()
                    elif task == 'pose':
                        self.actionPoint.trigger()
                    elif task == 'obb':
                        self.actionRBox.trigger()
                        
                    self.update_model_status(True, f"已加载 YOLO 模型: {display_name}")
                    self.btnPredict.show()
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    self.update_model_status(False, f"加载 YOLO 模型失败: {str(e)}")
                    self.btnPredict.hide()
            else:
                self.update_model_status(False, f"未找到对应的 YOLO 模型文件")
                self.btnPredict.hide()
        else:
            self.btnPredict.hide()
            self.current_yolo_predictor = None
            self.sam_client.cleanup()
            self.sam_client.current_model_key = key
            self.update_model_status(True, f"已选择模型: {display_name}。非 SAM 模型暂不支持智能推理。")
        
        supports_text = model_info.get("supports_text", False)
        
        # 只有不在点标注模式下才启用，因为点标注即使是SAM3也不可用
        if self.scene.mode != CanvasMode.POINT:
            self.samPromptInput.setEnabled(supports_text)
            self.samPromptBtn.setEnabled(supports_text)
            if supports_text:
                self.samPromptInput.setPlaceholderText("输入提示词提取 (如: dog)")
            else:
                self.samPromptInput.setPlaceholderText(f"{display_name} 不支持提示词")

    def edit_pose_template(self, name):
        dlg = SkeletonTemplateDialog(self, self.template_manager, self.is_dark_theme)
        dlg.load_template(name)
        dlg.name_edit.setText(name)
        if dlg.exec() == QDialog.Accepted:
            self._init_pose_templates()
            self.templateWidget._on_template_selected(name, f"{name} ▾")

    def delete_pose_template(self, name):
        from PySide6.QtWidgets import QMessageBox
        reply = QMessageBox.question(self, "删除骨架模板", f"确定要删除骨架模板 '{name}' 吗？", QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            self.template_manager.delete_template(name)
            self._init_pose_templates()
            self.templateWidget._on_template_selected("Person (COCO)", "Person (COCO) ▾")

    def _connect_signals(self):
        self.templateWidget.edit_template.connect(self.edit_pose_template)
        self.templateWidget.delete_template.connect(self.delete_pose_template)
        self.btnAuthorInfo.clicked.connect(self.show_author_info)
        self.btnCollapse.clicked.connect(self.toggle_sidebar)
        self.btnThemeToggle.clicked.connect(self.toggle_theme)

        self.btnUndo.clicked.connect(self.undo)
        self.btnRedo.clicked.connect(self.redo)
        self.btnDelete.clicked.connect(self.delete_selected)
        self.btnSave.clicked.connect(lambda: self.save_annotation(self.current_format))
        self.btnKeyboard.clicked.connect(self.show_help_dialog)

        self.actionOpen.triggered.connect(self.open_dir)

        # 下拉菜单组件信号
        self.formatWidget.format_changed.connect(self.set_current_format)

        self.btnDatasetTool.clicked.connect(self.open_dataset_tool)

        # self.actionFormatJSON.triggered.connect(lambda: self.set_current_format("json"))
        # self.actionFormatYOLO.triggered.connect(lambda: self.set_current_format("yolo"))
        # self.actionFormatXML.triggered.connect(lambda: self.set_current_format("xml"))

        self.actionRect.triggered.connect(lambda checked=False: self._set_mode(CanvasMode.RECT))
        self.actionPoly.triggered.connect(lambda checked=False: self._set_mode(CanvasMode.POLY))
        self.actionPoint.triggered.connect(lambda checked=False: self._set_mode(CanvasMode.POINT))
        self.actionRBox.triggered.connect(lambda checked=False: self._set_mode(CanvasMode.RBOX))

        self.templateWidget.template_changed.connect(self.on_pose_template_changed)

        self.samSwitch.toggled.connect(self.on_sam_toggled)

        # 同步顶部 Draw / Smart 按钮与 SAM 开关状态
        self.btnDrawMode.toggled.connect(lambda checked: self.samSwitch.setChecked(False) if checked else None)
        self.btnSmartMode.toggled.connect(lambda checked: self.samSwitch.setChecked(True) if checked else None)

        self.btnPredict.clicked.connect(self.on_predict_clicked)

        self.samPromptBtn.clicked.connect(self.trigger_sam_prompt)
        self.samPromptInput.returnPressed.connect(self.trigger_sam_prompt)

        self.listFiles.currentItemChanged.connect(self.on_file_selected)
        self.listFiles.customContextMenuRequested.connect(self.show_file_list_context_menu)
        self.scene.mouse_moved.connect(self.update_coordinate_label)
        self.scene.shape_drawn.connect(self.handle_new_shape)

        self.scene.shape_double_clicked.connect(self.edit_shape_label)  # 双击修改

        self.listClasses.itemChanged.connect(self.on_list_item_changed)

    def on_predict_clicked(self):
        if not self.current_image_path:
            DialogOver(self, "请先打开一张图片！", "提示", "warning")
            return
            
        if not getattr(self, 'current_yolo_predictor', None):
            DialogOver(self, "YOLO 模型未加载或初始化失败！", "提示", "warning")
            return
            
        if self.yolo_worker and self.yolo_worker.isRunning():
            return
            
        self.statusBar.showMessage("正在使用 YOLO 进行预测...", 3000)
        self.helpLabel.setText("正在使用 YOLO 进行预测...")
        self.helpLabel.setStyleSheet("color: orange;")
        self.original_predict_text = self.btnPredict.text()
        self.btnPredict.setText("预测中")
        
        self.predict_movie.start()
        self.btnPredict.setEnabled(False)
        
        self.yolo_worker = YoloPredictorWorker(self.current_yolo_predictor, self.current_image_path)
        self.yolo_worker.finished.connect(self.on_predict_finished)
        self.yolo_worker.error.connect(self.on_predict_error)
        self.yolo_worker.start()

    def on_predict_finished(self, shapes):
        self.predict_movie.stop()
        self.btnPredict.setText(self.original_predict_text)
        self.btnPredict.setIcon(QIcon("ui/icon/lightning-fill.svg"))
        self.btnPredict.setEnabled(True)
        
        if not shapes:
            DialogOver(self, "未找到任何预测结果", "提示", "info")
            self.statusBar.showMessage("预测完成，未找到任何结果", 3000)
            self._update_help_text(self.scene.mode)
            return
            
        # Get existing shapes for deduplication
        existing_shapes = []
        for item in self.scene.items():
            from core.shapes import BaseShape
            if isinstance(item, BaseShape) and not getattr(item, 'is_temp', False):
                existing_shapes.append(item)
            
        # Add shapes
        class_counts = {}
        added_count = 0
        
        for s in shapes:
            shape_type = s["type"]
            label = s["label"]
            data = s["data"]
            
            # Deduplication logic (IoU > 0.8)
            new_rect = None
            if shape_type == "rect":
                new_rect = data
            elif shape_type in ["poly", "rbox"]:
                new_rect = data.boundingRect()
            elif shape_type == "pose":
                new_rect = data["rect"]
                
            is_duplicate = False
            if new_rect:
                for ext_shape in existing_shapes:
                    if getattr(ext_shape, 'label', '') != label:
                        continue
                    ext_rect = ext_shape.sceneBoundingRect()
                    
                    intersection = new_rect.intersected(ext_rect)
                    inter_area = max(0, intersection.width()) * max(0, intersection.height())
                    area1 = max(0, new_rect.width()) * max(0, new_rect.height())
                    area2 = max(0, ext_rect.width()) * max(0, ext_rect.height())
                    union_area = area1 + area2 - inter_area
                    
                    if union_area > 0 and (inter_area / union_area) > 0.8:
                        is_duplicate = True
                        break
                        
            if is_duplicate:
                continue
            
            added_count += 1
            class_counts[label] = class_counts.get(label, 0) + 1
            
            if label not in self.class_list:
                self.add_class_to_list(label)
                
            new_shape = None
            if shape_type == "rect":
                new_shape = RectShape(data, label)
            elif shape_type == "poly" or shape_type == "rbox":
                new_shape = PolyShape(data, label)
            elif shape_type == "pose":
                rect = data["rect"]
                kps = data["keypoints"]
                
                # 优先使用模型预测结果中自带的骨架连接逻辑 (YOLO 官方定义)
                if "skeleton" in s:
                    yolo_skeleton = s["skeleton"]
                    kpt_names = s.get("kpt_names", [])
                    
                    # 构造临时模板，直接遵循 YOLO 官网/模型的连接定义
                    template = {
                        "name": f"YOLO_Auto_{len(kps)}",
                        "label": label,
                        "keypoints": [],
                        "connections": []
                    }
                    
                    # 填充点名称
                    for i in range(len(kps)):
                        name = kpt_names[i] if i < len(kpt_names) else f"kp_{i}"
                        template["keypoints"].append({"name": name, "color": "#00FF00", "default_pos": [0.5, 0.5]})
                        
                    # 填充连接线 (YOLO 官网通常是 1-based 索引，需要转为 0-based)
                    for edge in yolo_skeleton:
                        if len(edge) == 2:
                            p1, p2 = edge[0], edge[1]
                            # 自动检测并转换 1-based 到 0-based
                            if p1 > 0 and p2 > 0:
                                template["connections"].append([p1 - 1, p2 - 1])
                            else:
                                template["connections"].append([p1, p2])
                else:
                    # 兜底方案：尝试从本地模板库匹配
                    template = self.scene.current_pose_template
                    if not template or len(template.get("keypoints", [])) != len(kps):
                        found_template = False
                        for t in self.template_manager.templates:
                            if len(t.get("keypoints", [])) == len(kps):
                                template = t
                                found_template = True
                                break
                        
                        if not found_template:
                            template = {"name": f"YOLO_Pose_{len(kps)}", "keypoints": [], "connections": []}
                            for i in range(len(kps)):
                                template["keypoints"].append({"name": f"kp_{i}", "color": "#00FF00", "default_pos": [0.5, 0.5]})
                        
                new_shape = PoseShape(rect, template, label)
                for i, kp in enumerate(kps):
                    if i < len(new_shape.kps):
                        local_pt = new_shape.mapFromScene(kp["pos"])
                        new_shape.kps[i].setPos(local_pt)
                        new_shape.kps[i].set_visibility(kp["vis"])
                
                new_shape.update_bounding_box()
                new_shape.update_lines()
                
            if new_shape:
                self.scene.addItem(new_shape)
                if hasattr(new_shape, 'update_label_text'):
                    new_shape.update_label_text(label)
                if hasattr(new_shape, 'update_label_position'):
                    new_shape.update_label_position(new_shape)
                    
        if added_count == 0:
            DialogOver(self, "暂无新的预测内容需要添加", "提示", "info")
            self.statusBar.showMessage("预测完成，暂无新内容", 3000)
            self._update_help_text(self.scene.mode)
            return
        
        self.save_classes()
        self.push_state()
        self.auto_save_annotation()
        
        # Format message
        msg_parts = [f"{cls} ({count})" for cls, count in class_counts.items()]
        msg = "添加了" + str(added_count) + "个目标：" + ", ".join(msg_parts)
        DialogOver(self, msg + "，并进行标注", "预测成功", "success")
        self.statusBar.showMessage(msg, 5000)
        self._update_help_text(self.scene.mode)

    def on_predict_error(self, err_msg):
        self.predict_movie.stop()
        self.btnPredict.setText(self.original_predict_text)
        self.btnPredict.setIcon(QIcon("ui/icon/lightning-fill.svg"))
        self.btnPredict.setEnabled(True)
        DialogOver(self, f"预测失败: {err_msg}", "错误", "danger")
        self.statusBar.showMessage("预测出错", 3000)
        self._update_help_text(self.scene.mode)

    def add_class_to_list(self, cls_name):
        """列表项支持双击编辑"""
        if cls_name not in self.class_list:
            self.class_list.append(cls_name)
            item = QListWidgetItem(cls_name)
            # 开启双击编辑权限
            item.setFlags(item.flags() | Qt.ItemIsEditable)
            item.setData(Qt.UserRole, cls_name)
            self.listClasses.addItem(item)

    def push_state(self):
        """把当前画布状态拍个快照，存进撤销堆栈"""
        if not self.current_image_path: return
        current_state = Exporter.extract_shapes(self.scene)

        # 如果拖了一下鼠标但什么都没变，就不存，节约内存
        if self.undo_stack:
            last_state = self.undo_stack[-1]
            if json.dumps(last_state, sort_keys=True) == json.dumps(current_state, sort_keys=True):
                return

        self.undo_stack.append(current_state)
        # 限制最大步数
        if len(self.undo_stack) > self.max_history_steps:
            self.undo_stack.pop(0)

        # 一旦有新操作，重做（前进）堆栈必须清空
        self.redo_stack.clear()
        self.update_undo_redo_buttons()

    def undo(self):
        """撤销 (Ctrl+Z)"""
        if len(self.undo_stack) > 1:
            # 把现在的状态拿出来，放到重做栈里去
            current_state = self.undo_stack.pop()
            self.redo_stack.append(current_state)
            # 获取上一步的状态并还原
            previous_state = self.undo_stack[-1]
            self.restore_state(previous_state)
            self.update_undo_redo_buttons()

    def redo(self):
        """重做/前进 (Ctrl+Y 或 Ctrl+Shift+Z)"""
        if self.redo_stack:
            # 从重做栈里拿出来，塞回撤销栈
            next_state = self.redo_stack.pop()
            self.undo_stack.append(next_state)
            # 还原该状态
            self.restore_state(next_state)
            self.update_undo_redo_buttons()
            
    def update_undo_redo_buttons(self):
        """更新撤销和重做按钮的可用状态"""
        self.btnUndo.setEnabled(len(self.undo_stack) > 1)
        self.btnRedo.setEnabled(len(self.redo_stack) > 0)
        
        # 强制刷新图标渲染
        from PySide6.QtGui import QIcon
        self.btnUndo.setIcon(self.set_icon_color(QIcon("ui/icon/arrow-u-up-left.svg"), self.current_icon_color))
        self.btnRedo.setIcon(self.set_icon_color(QIcon("ui/icon/arrow-u-up-right.svg"), self.current_icon_color))

    def restore_state(self, state):
        """根据快照数据，完全重建画板元素"""
        
        # 记录当前选中状态以便恢复
        selected_labels_or_poses = []
        for item in self.scene.selectedItems():
            if isinstance(item, PoseShape):
                # We can't perfectly match pose instances across re-creation easily, 
                # but we can try to match by rect center and label
                selected_labels_or_poses.append({"type": "pose", "center": item.pos()})
            elif hasattr(item, 'label'):
                selected_labels_or_poses.append({"type": "other", "label": item.label})

        self.scene.clear_shapes()
        for shape_data in state:
            label = shape_data.get("label", "")
            shape_type = shape_data.get("type", "")
            points = shape_data.get("points", [])

            # 同步类别列表
            if label and label not in self.class_list:
                # self.class_list.append(label)
                # self.listClasses.addItem(label)
                self.add_class_to_list(label)
                self.save_classes()

            shape = None
            if shape_type == "rectangle" and len(points) == 2:
                rect = QRectF(points[0][0], points[0][1], points[1][0] - points[0][0], points[1][1] - points[0][1])
                shape = RectShape(rect, label)
            elif shape_type == "polygon" and len(points) >= 3:
                qpoints = [QPointF(p[0], p[1]) for p in points]
                shape = PolyShape(QPolygonF(qpoints), label)
            elif shape_type == "point" and len(points) == 1:
                shape = PointShape(QPointF(points[0][0], points[0][1]), label)
            elif shape_type == "obb":
                rect_data = shape_data.get("rect")
                angle = shape_data.get("angle", 0)
                if rect_data and len(rect_data) == 4:
                    cx, cy, w, h = rect_data[0], rect_data[1], rect_data[2], rect_data[3]
                    shape = RotatedRectShape(cx, cy, w, h, angle, label)
            elif shape_type == "pose":
                rect_data = shape_data.get("rect")
                template_name = shape_data.get("template_name", "")
                kps_data = shape_data.get("keypoints", [])
                if rect_data and len(rect_data) == 4:
                    cx, cy, w, h = rect_data
                    angle = shape_data.get("angle", 0)
                    template = self.template_manager.get_template(template_name)
                    if not template:
                        template = {"name": template_name, "keypoints": [], "connections": []}
                        for i, kp in enumerate(kps_data):
                            template["keypoints"].append({"name": f"kp_{i}", "color": "#00FF00", "default_pos": [0.5, 0.5]})
                    
                    shape = PoseShape(QRectF(cx - w / 2, cy - h / 2, w, h), template, label)
                    shape.setRotation(angle)
                    for i, kp_data in enumerate(kps_data):
                        if i < len(shape.kps):
                            local_pt = shape.mapFromScene(QPointF(kp_data[0], kp_data[1]))
                            shape.kps[i].setPos(local_pt)
                            shape.kps[i].set_visibility(kp_data[2])
                    shape.update_lines()

            if shape:
                self.scene.addItem(shape)
                if hasattr(shape, 'update_label_text'):
                    shape.update_label_text(label)
                if hasattr(shape, 'update_label_position'):
                    shape.update_label_position(shape)
                
                # 尝试恢复选中状态 (保持编辑模式)
                is_selected = False
                if shape_type == "pose":
                    for s in selected_labels_or_poses:
                        if s["type"] == "pose" and (s["center"] - shape.pos()).manhattanLength() < 5:
                            is_selected = True
                            break
                else:
                    for s in selected_labels_or_poses:
                        if s["type"] == "other" and s["label"] == label:
                            is_selected = True
                            break
                
                shape.setSelected(is_selected)
                
                if hasattr(shape, 'update_label_visibility'):
                    shape.update_label_visibility(shape, is_selected=is_selected, is_hovered=False)
                if hasattr(shape, '_update_handle_visibility'):
                    shape._update_handle_visibility()

        self.auto_save_annotation()

    def open_dataset_tool(self):
        try:
            if not hasattr(self, 'dataset_window') or self.dataset_window is None:
                self.dataset_window = DatasetToolWindow()
            # 显示窗口
            self.dataset_window.show()
            # 把窗口强制拉到最前面
            self.dataset_window.raise_()
            self.dataset_window.activateWindow()

        except Exception as e:
            DialogOver(self, f"启动失败: {e}", "系统错误", "danger")

    def trigger_sam_prompt(self):
        if self.scene.mode == CanvasMode.POINT:
            DialogOver(self, "点标注模式下无法使用 SAM 智能提取", "提示", "warning")
            return

        prompt = self.samPromptInput.text().strip()
        if prompt:
            self.samSwitch.setChecked(True)
            self.helpLabel.setText(f"正在提取提示词: {prompt}...")
            self.helpLabel.setStyleSheet("color: orange;")
            self.sam_client.request_text_inference(prompt)

    def handle_text_results(self, results, prompt_text):
        if not results:
            self.helpLabel.setText(f"提取完成: 未发现关于 '{prompt_text}' 的目标")
            self.helpLabel.setStyleSheet("color: red;")
            return

        self.helpLabel.setText(f"提取完成: 成功抓取 {len(results)} 个 '{prompt_text}' 目标")
        self.helpLabel.setStyleSheet("color: green;")

        if prompt_text not in self.class_list:
            # self.class_list.append(prompt_text)
            # self.listClasses.addItem(prompt_text)
            self.add_class_to_list(prompt_text)
            self.save_classes()

        for res in results:
            if self.scene.mode == CanvasMode.RECT:
                x, y, w, h = res["rect"]
                shape = RectShape(QRectF(x, y, w, h), prompt_text)
            else:
                qpts = [QPointF(p[0], p[1]) for p in res["poly_pts"]]
                shape = PolyShape(QPolygonF(qpts), prompt_text)

            self.scene.addItem(shape)
            if hasattr(shape, 'update_label_text'):
                shape.update_label_text(prompt_text)
            if hasattr(shape, 'update_label_position'):
                shape.update_label_position(shape)
            if hasattr(shape, 'update_label_visibility'):
                shape.update_label_visibility(shape, is_selected=False, is_hovered=False)

        self.auto_save_annotation()

    def delete_selected(self):
        for item in self.scene.selectedItems():
            self.scene.removeItem(item)
        self.scene.state_changed.emit()

    def toggle_sidebar(self):
        if self.toolBar.toolButtonStyle() == Qt.ToolButtonTextBesideIcon:
            # 隐藏文字 — 收缩模式
            self.toolBar.setToolButtonStyle(Qt.ToolButtonIconOnly)
            self.toolBar.setFixedWidth(50)
            self.logoLabel.hide()
            
            # 收缩模式：缩小 Logo 图标
            logo_path = "ui/icon/logo.png"
            pix = QPixmap(logo_path).scaled(24, 24, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.logoIcon.setPixmap(pix)
            self.logoIcon.setFixedSize(24, 24)

            # 释放按钮宽度限制
            for btn in self._actionButtons:
                btn.setMinimumWidth(0)

            self.formatWidget.set_icon_only(True)
            self.btnDatasetTool.setText("")
            self.samIcon.hide() # 收缩时隐藏左侧图标
            # SAM 开关竖向显示
            self.samSwitch.setFixedSize(26, 50)
            self.samSwitch._vertical = True
            self.samSwitch.update()
        else:
            # 显示文字 — 展开模式
            self.toolBar.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            self.toolBar.setFixedWidth(190)
            
            # 展开模式：恢复 Logo 图标和文字
            self.logoLabel.show()
            self.logoLabel.setText("LabelPaw")
            logo_path = "ui/icon/logo.png"
            pix = QPixmap(logo_path).scaled(28, 28, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.logoIcon.setPixmap(pix)
            self.logoIcon.setFixedSize(28, 28)

            # 恢复按钮宽度
            for btn in self._actionButtons:
                btn.setMinimumWidth(180)

            self.formatWidget.set_icon_only(False)
            self.btnDatasetTool.setText(" 数据集处理")
            self.samIcon.show() # 展开时显示左侧图标
            # SAM 开关横向显示
            self.samSwitch.setFixedSize(50, 26)
            self.samSwitch._vertical = False
            self.samSwitch.update()

    def set_icon_color(self, icon, color):
        from PySide6.QtGui import QPainter, QIcon, QPixmap, QColor
        pixmap = icon.pixmap(100, 100)
        painter = QPainter(pixmap)
        painter.setCompositionMode(QPainter.CompositionMode_SourceIn)
        painter.fillRect(pixmap.rect(), color)
        painter.end()
        
        # 核心逻辑：自己创建一个可以响应 enable/disable 的 QIcon
        # 我们把这个纯色图作为 Normal 状态
        new_icon = QIcon()
        new_icon.addPixmap(pixmap, QIcon.Normal, QIcon.On)
        new_icon.addPixmap(pixmap, QIcon.Normal, QIcon.Off)
        
        # 为了让禁用状态变灰，我们用 QPainter 绘制一个半透明的版本作为 Disabled 状态
        disabled_pixmap = icon.pixmap(100, 100)
        dpainter = QPainter(disabled_pixmap)
        dpainter.setCompositionMode(QPainter.CompositionMode_SourceIn)
        
        # 深色主题下禁用颜色为半透明白，浅色主题下为半透明黑
        if color.lightness() > 128:
            disabled_color = QColor(255, 255, 255, 60)
        else:
            disabled_color = QColor(15, 23, 42, 60)
            
        dpainter.fillRect(disabled_pixmap.rect(), disabled_color)
        dpainter.end()
        
        new_icon.addPixmap(disabled_pixmap, QIcon.Disabled, QIcon.On)
        new_icon.addPixmap(disabled_pixmap, QIcon.Disabled, QIcon.Off)
        
        return new_icon

    def toggle_theme(self):
        self.is_dark_theme = not self.is_dark_theme
        if self.is_dark_theme:
            self.setStyleSheet(DARK_THEME)
            self.btnThemeToggle.setText("☀")
            
            # 更新深色图标
            from PySide6.QtGui import QIcon, QColor
            self.current_icon_color = QColor(255, 255, 255)
            self.btnUndo.setIcon(self.set_icon_color(QIcon("ui/icon/arrow-u-up-left.svg"), self.current_icon_color))
            self.btnRedo.setIcon(self.set_icon_color(QIcon("ui/icon/arrow-u-up-right.svg"), self.current_icon_color))
            self.btnDelete.setIcon(self.set_icon_color(QIcon("ui/icon/trash.svg"), self.current_icon_color))
            self.btnSave.setIcon(self.set_icon_color(QIcon("ui/icon/floppy-disk.svg"), self.current_icon_color))
            self.btnKeyboard.setIcon(self.set_icon_color(QIcon("ui/icon/keyboard.svg"), self.current_icon_color))
            
            # 更新侧边栏图标
            self.actionOpen.setIcon(self.set_icon_color(QIcon("ui/icon/folder.svg"), self.current_icon_color))
            self.actionRect.setIcon(self.set_icon_color(QIcon("ui/icon/rectangle.svg"), self.current_icon_color))
            self.actionPoly.setIcon(self.set_icon_color(QIcon("ui/icon/polygon.svg"), self.current_icon_color))
            self.actionPoint.setIcon(self.set_icon_color(QIcon("ui/icon/关键点.svg"), self.current_icon_color))
            self.actionRBox.setIcon(self.set_icon_color(QIcon("ui/icon/手机旋转1.svg"), self.current_icon_color))
            self.samIcon.setPixmap(self.set_icon_color(QIcon("ui/icon/魔法-copy.svg"), self.current_icon_color).pixmap(24, 24))
            self.btnDatasetTool.setIcon(self.set_icon_color(QIcon("ui/icon/wrench.svg"), self.current_icon_color))
            self.formatWidget.btn.setIcon(self.set_icon_color(QIcon("ui/icon/格式.svg"), self.current_icon_color))
        else:
            self.setStyleSheet(LIGHT_THEME)
            self.btnThemeToggle.setText("🌙")
            
            # 更新浅色图标
            from PySide6.QtGui import QIcon, QColor
            self.current_icon_color = QColor(15, 23, 42) # 深灰色
            self.btnUndo.setIcon(self.set_icon_color(QIcon("ui/icon/arrow-u-up-left.svg"), self.current_icon_color))
            self.btnRedo.setIcon(self.set_icon_color(QIcon("ui/icon/arrow-u-up-right.svg"), self.current_icon_color))
            self.btnDelete.setIcon(self.set_icon_color(QIcon("ui/icon/trash.svg"), self.current_icon_color))
            self.btnSave.setIcon(self.set_icon_color(QIcon("ui/icon/floppy-disk.svg"), self.current_icon_color))
            self.btnKeyboard.setIcon(self.set_icon_color(QIcon("ui/icon/keyboard.svg"), self.current_icon_color))
            
            # 更新侧边栏图标
            self.actionOpen.setIcon(self.set_icon_color(QIcon("ui/icon/folder.svg"), self.current_icon_color))
            self.actionRect.setIcon(self.set_icon_color(QIcon("ui/icon/rectangle.svg"), self.current_icon_color))
            self.actionPoly.setIcon(self.set_icon_color(QIcon("ui/icon/polygon.svg"), self.current_icon_color))
            self.actionPoint.setIcon(self.set_icon_color(QIcon("ui/icon/关键点.svg"), self.current_icon_color))
            self.actionRBox.setIcon(self.set_icon_color(QIcon("ui/icon/手机旋转1.svg"), self.current_icon_color))
            self.samIcon.setPixmap(self.set_icon_color(QIcon("ui/icon/魔法-copy.svg"), self.current_icon_color).pixmap(24, 24))
            self.btnDatasetTool.setIcon(self.set_icon_color(QIcon("ui/icon/wrench.svg"), self.current_icon_color))
            self.formatWidget.btn.setIcon(self.set_icon_color(QIcon("ui/icon/格式.svg"), self.current_icon_color))

    def show_author_info(self):
        dialog = AuthorInfoDialog(self)
        dialog.exec()

    def show_help_dialog(self):
        help_text = """
        <h3>【快捷键大全】</h3>
        <ul>
            <li><b>A / 左方向键</b>：上一张图片</li>
            <li><b>D / 右方向键</b>：下一张图片</li>
            <li><b style="color:red;">Ctrl + S</b>：保存当前标注</li>
            <li><b style="color:blue;">Q</b>：开启/关闭 SAM 智能辅助</li>
            <li><b>R</b>：切换至 矩形标注</li>
            <li><b>P</b>：切换至 多边形标注</li>
            <li><b>T</b>：切换至 关键点标注</li>
            <li><b>O</b>：切换至 旋转框标注</li>
            <li><b>M</b>：触发 模型预测 (Smart 模式并加载模型时可用)</li>
            <li><b>Del / Backspace</b>：删除当前选中的标注框</li>
            <li><b>F1</b>：打开此帮助文档</li>
        </ul>
        <hr>
        <h3>【多边形绘制技巧】</h3>
        <ul>
            <li><b>左键点击</b>：添加顶点</li>
            <li><b>Ctrl + Z</b>：撤销上一个顶点</li>
            <li><b>双击 / Enter</b>：闭合多边形</li>

        </ul>
        <hr>
        <h3>【旋转框绘制快捷键】</h3>
        <ul>
            <li><b> Z / V</b>：每次向左/向右旋转 5°</li>
            <li><b>X / C</b>：每次向左/向右旋转 1°</li>
        </ul>
        <hr>
        <h3>【SAM 智能辅助】</h3>
        <ul>
            <li><b>鼠标点选</b>：开启开关后，鼠标悬停预览，点击直接确认生成高精度轮廓。</li>
            <li><b>提示词提取</b>：在右下角输入框输入目标名称（如: dog），按回车即可一键全图抓取并打好框！左侧选中的是“矩形”还是“多边形”格式。</li>
        </ul>
        """
        QMessageBox.about(self, "LabelPaw 使用说明", help_text)

    def update_coordinate_label(self, x, y):
        self.coordLabel.setText(f"坐标: X: {x}, Y: {y}")

    def on_sam_toggled(self, checked):
        if checked and self.scene.mode == CanvasMode.POINT:
            yolo_pred = getattr(self, "current_yolo_predictor", None)
            is_yolo_pose = yolo_pred is not None and getattr(yolo_pred, "task", "") == "pose"
            
            if not is_yolo_pose:
                # 尝试寻找并加载默认的 YOLO pose 模型
                import sys
                if getattr(sys, 'frozen', False):
                    PROJECT_ROOT = os.path.dirname(sys.executable)
                else:
                    PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
                LOCAL_WEIGHTS_DIR = os.path.join(PROJECT_ROOT, "weights")
                HARDCODED_DEV_DIR = r"E:\11-AI\标注工具\weights"
                
                if os.path.exists(LOCAL_WEIGHTS_DIR):
                    model_base_dir = LOCAL_WEIGHTS_DIR
                elif os.path.exists(HARDCODED_DEV_DIR):
                    model_base_dir = HARDCODED_DEV_DIR
                else:
                    model_base_dir = LOCAL_WEIGHTS_DIR
                
                default_pose_path = None
                default_type = ""
                default_name = ""
                
                if os.path.exists(model_base_dir):
                    for item in os.listdir(model_base_dir):
                        item_path = os.path.join(model_base_dir, item)
                        if os.path.isdir(item_path) and item.startswith("yolo") and item.endswith("_weights"):
                            for f in os.listdir(item_path):
                                if f.endswith('-pose.pt') or f.endswith('-pose.onnx'):
                                    default_pose_path = os.path.join(item_path, f)
                                    default_name = f.replace('.pt', '').replace('.onnx', '')
                                    default_type = item.replace('_weights', '')
                                    break
                        if default_pose_path:
                            break
                            
                if default_pose_path and os.path.exists(default_pose_path):
                    self.statusBar.showMessage(f"正在为您自动切换至 {default_name} 模型...", 3000)
                    self.on_model_selected({
                        "key": default_name,
                        "display_name": default_name,
                        "type": default_type,
                        "path": default_pose_path
                    })
                else:
                    # 不再强行拦截，而是提醒用户切换模型，这样模型选择器才能显示出来
                    self.statusBar.showMessage("当前为点标注模式，请在上方选择 YOLO 姿态模型以使用智能辅助", 4000)
                    self.helpLabel.setText("请选择 YOLO 姿态模型")
                    self.helpLabel.setStyleSheet("color: orange;")

        self.scene.set_sam_enabled(checked)
        self._update_help_text(self.scene.mode)
        
        # 同步更新顶部工具栏按钮
        if checked:
            self.btnModelSelector.show()
            if getattr(self, 'current_yolo_predictor', None) is not None:
                self.btnPredict.show()
            if not self.btnSmartMode.isChecked():
                self.btnSmartMode.setChecked(True)
        else:
            self.btnModelSelector.hide()
            self.btnPredict.hide()
            if not self.btnDrawMode.isChecked():
                self.btnDrawMode.setChecked(True)

        if self.scene.mode == CanvasMode.POINT:
            if checked:
                self.templateWidget.hide()
                self.sepTemplate.hide()
            else:
                self.templateWidget.show()
                self.sepTemplate.show()

    def on_pose_template_changed(self, text):
        if not text: return
        if text == "+ New Template...":
            dlg = SkeletonTemplateDialog(self, self.template_manager, self.is_dark_theme)
            if dlg.exec() == QDialog.Accepted:
                self._init_pose_templates()
                # Select the newly created template
                last_template = self.template_manager.get_template_names()[-1]
                self.templateWidget._on_template_selected(last_template, f"{last_template} ▾")
            else:
                self.templateWidget._on_template_selected("Person (COCO)", "Person (COCO) ▾")
        else:
            self.scene.current_pose_template = self.template_manager.get_template(text)
            self._update_help_text(self.scene.mode)
            self.formatWidget.set_yolo_enabled(True)
        # Ensure we are in point mode
        self._set_mode(CanvasMode.POINT)

    def _set_mode(self, mode):
        self.scene.set_mode(mode)
        mode_name = CanvasMode.get_mode_name(mode)
        self.modeLabel.setText(f"模式: {mode_name}标注")
        self._update_help_text(mode)

        if mode == CanvasMode.RECT:
            self.actionRect.setChecked(True)
        elif mode == CanvasMode.POLY:
            self.actionPoly.setChecked(True)
        elif mode == CanvasMode.POINT:
            self.actionPoint.setChecked(True)
        elif mode == CanvasMode.RBOX:
            self.actionRBox.setChecked(True)

        if mode == CanvasMode.POINT:
            yolo_pred = getattr(self, 'current_yolo_predictor', None)
            is_yolo_pose = yolo_pred is not None and getattr(yolo_pred, 'task', '') == 'pose'

            if self.samSwitch.isChecked():
                self.templateWidget.hide()
                self.sepTemplate.hide()
            else:
                self.templateWidget.show()
                self.sepTemplate.show()

            self.samSwitch.setEnabled(True)
            
            # 移除自动开启逻辑，保持用户当前的手动/智能选择
            
            self.samPromptInput.setEnabled(False)
            self.samPromptBtn.setEnabled(False)
            self.samPromptInput.setPlaceholderText("点标注模式下文本提示不可用")
            
            if not self.scene.current_pose_template and not is_yolo_pose:
                self.formatWidget.set_yolo_enabled(False)
            else:
                self.formatWidget.set_yolo_enabled(True)
        else:
            self.templateWidget.hide()
            self.sepTemplate.hide()
            self.samSwitch.setEnabled(True)
            
            supports_text = self.sam_client.supports_text_prompt()
            self.samPromptInput.setEnabled(supports_text)
            self.samPromptBtn.setEnabled(supports_text)
            
            if supports_text:
                self.samPromptInput.setPlaceholderText("输入提示词提取 (如: dog)")
            else:
                self.samPromptInput.setPlaceholderText("当前模型不支持提示词")
                
            self.formatWidget.set_yolo_enabled(True)

    def _update_help_text(self, mode):
        is_sam = self.samSwitch.isChecked()
        if mode == CanvasMode.RECT:
            if is_sam:
                self.helpLabel.setText("操作: 鼠标悬停实时预览外接矩形，左键点击直接确认生成矩形框")
            else:
                self.helpLabel.setText("操作: 拖动鼠标绘制常规矩形")
        elif mode == CanvasMode.POLY:
            if is_sam:
                self.helpLabel.setText("操作: 鼠标悬停实时预览轮廓节点，左键点击直接确认生成多边形")
            else:
                self.helpLabel.setText("操作: 点击添加顶点，双击闭合多边形")
        elif mode == CanvasMode.POINT:
            self.helpLabel.setText("操作: 点击放置骨架模板，选定后可通过手柄拖拽放大、旋转或微调关键点")
        elif mode == CanvasMode.RBOX:
            self.helpLabel.setText("操作: 拖动绘制旋转框，Z/X/C/V调整角度")

    def load_classes(self, dir_path):
        self.class_list.clear()
        self.listClasses.clear()
        class_file = os.path.join(dir_path, "classes.txt")
        if os.path.exists(class_file):
            with open(class_file, "r", encoding="utf-8") as f:
                for line in f:
                    cls_name = line.strip()
                    if cls_name:
                        self.add_class_to_list(cls_name)
                        # self.class_list.append(cls_name)
                        # self.listClasses.addItem(cls_name)

    def save_classes(self):
        if self.current_dir:
            class_file = os.path.join(self.current_dir, "classes.txt")
            with open(class_file, "w", encoding="utf-8") as f:
                f.write("\n".join(self.class_list))

    def handle_new_shape(self, shape):
        self.scene.addItem(shape)
        QApplication.processEvents()

        from core.shapes import PoseShape
        if isinstance(shape, PoseShape) and shape.template:
            # 骨架模板自动使用模板标签作为类别，跳过弹窗
            cls_name = shape.template.get("label", shape.template.get("name", "Unknown"))
            ok = True
        else:
            last_class = self.class_list[-1] if self.class_list else ""
            from ui.label_dialog import LabelDialog
            dlg = LabelDialog("输入类别", "请选择或输入类别名称:", self.class_list, last_class, self.is_dark_theme, self)
            ok = dlg.exec()
            cls_name = dlg.get_text()

        if ok and cls_name:
            cls_name = cls_name.strip()
            if cls_name not in self.class_list:
                # self.class_list.append(cls_name)
                # self.listClasses.addItem(cls_name)
                self.add_class_to_list(cls_name)
                self.save_classes()

            shape.label = cls_name
            if hasattr(shape, 'update_label_text'):
                shape.update_label_text(cls_name)
            if hasattr(shape, 'update_label_position'):
                shape.update_label_position(shape)
            if hasattr(shape, 'update_label_visibility'):
                shape.update_label_visibility(shape, is_selected=True, is_hovered=False)
            for item in self.scene.selectedItems():
                item.setSelected(False)
            shape.setSelected(True)
            self.push_state()
        else:
            self.scene.removeItem(shape)

    def edit_shape_label(self, shape):
        """二次修改已有标注框的类别"""
        current_label = shape.label
        from ui.label_dialog import LabelDialog
        dlg = LabelDialog("修改类别", "请重新选择或输入类别名称:", self.class_list, current_label, self.is_dark_theme, self)
        ok = dlg.exec()
        cls_name = dlg.get_text()

        if ok and cls_name:
            cls_name = cls_name.strip()
            if cls_name not in self.class_list:
                # self.class_list.append(cls_name)
                # self.listClasses.addItem(cls_name)
                self.add_class_to_list(cls_name)
                self.save_classes()

            # 更新形状的数据和标签显示
            shape.label = cls_name
            if hasattr(shape, 'update_label_text'):
                shape.update_label_text(cls_name)

            # 修改完自动保存一次
            self.auto_save_annotation()
            self.push_state()

    def on_list_item_changed(self, item):
        """处理右侧列表双击修改类别名的全局涟漪效应"""
        new_name = item.text().strip()
        old_name = item.data(Qt.UserRole)

        # 如果没有真正改动，直接跳过
        if not old_name or new_name == old_name:
            return

        self.listClasses.blockSignals(True)
        try:
            if not new_name:
                DialogOver(self, "类别名不能为空！", "名称错误", "warning")
                item.setText(old_name)
                return

            if new_name in self.class_list:
                DialogOver(self, f"类别名 '{new_name}' 已存在！", "名称冲突", "warning")
                item.setText(old_name)
                return

            # 替换内部字典
            idx = self.class_list.index(old_name)
            self.class_list[idx] = new_name
            item.setData(Qt.UserRole, new_name)  # 把新名字设为基准

            # 遍历画板，把所有旧名字的框换成新名字
            changed = False
            for shape in self.scene.items():
                if isinstance(shape, (RectShape, PolyShape, PointShape, RotatedRectShape)):
                    if getattr(shape, 'label', '') == old_name:
                        shape.label = new_name
                        if hasattr(shape, 'update_label_text'):
                            shape.update_label_text(new_name)
                        changed = True

            # 保存并推入时光机
            self.save_classes()
            if changed:
                self.auto_save_annotation()
                self.push_state()

            DialogOver(self, f"已将所有的 '{old_name}' 批量变更为 '{new_name}'", "修改成功", "success")

        finally:
            self.listClasses.blockSignals(False)

    def open_dir(self):
        dir_path = QFileDialog.getExistingDirectory(self, "选择图片目录")
        if dir_path:
            self.current_dir = dir_path
            self.listFiles.clear()
            self.load_classes(dir_path)
            for f in os.listdir(dir_path):
                if f.lower().endswith(('.png', '.jpg', '.jpeg', '.bmp')):
                    self.listFiles.addItem(os.path.join(dir_path, f))

            if self.listFiles.count() > 0:
                self.listFiles.setCurrentRow(0)

    # def update_model_status(self, success, msg):
    #     self.helpLabel.setText(msg)
    #     if success:
    #         self.helpLabel.setStyleSheet("color: green;")
    #     else:
    #         self.helpLabel.setStyleSheet("color: red;")

    def update_model_status(self, success, msg):
        self.helpLabel.setText(msg)
        if success:
            self.helpLabel.setStyleSheet("color: green;")
            # 模型加载成功后，检查用户是不是已经提前打开图片了
            if self.current_image_path:
                # self.helpLabel.setText("模型已就绪，正在自动分析当前图片特征...")
                # self.helpLabel.setStyleSheet("color: orange;")
                QApplication.processEvents()
                self.sam_client.set_image(self.current_image_path)
                # self.helpLabel.setText("分析完成，可以开始智能标注")
                # self.helpLabel.setStyleSheet("color: green;")
        else:
            self.helpLabel.setStyleSheet("color: red;")

    def show_file_list_context_menu(self, pos):
        """显示文件列表右键菜单"""
        selected_items = self.listFiles.selectedItems()
        if not selected_items:
            return

        menu = QMenu()
        # 应用主题样式
        if self.is_dark_theme:
            menu.setStyleSheet("QMenu { background-color: #2b2b2b; color: #fff; border: 1px solid #444; }")
        
        count = len(selected_items)
        delete_action = menu.addAction(f"删除选中的 {count} 个文件")
        delete_action.setIcon(QIcon("ui/icon/trash.svg"))
        
        action = menu.exec(self.listFiles.mapToGlobal(pos))
        if action == delete_action:
            self.delete_selected_files(selected_items)

    def delete_selected_files(self, items):
        """批量删除选中的图片及其对应的标签文件"""
        count = len(items)

        # 记录需要删除的路径
        paths_to_delete = [item.text() for item in items]
        
        # 如果当前正在查看的文件被删除了，需要切换
        current_deleted = self.current_image_path in paths_to_delete
        
        # 暂时断开信号避免删除过程中触发切换和自动保存
        self.listFiles.currentItemChanged.disconnect(self.on_file_selected)

        if current_deleted:
            self.scene.clear_shapes()
            self.scene.img_item = None
            self.current_image_path = None

        deleted_count = 0
        for item, img_path in zip(items, paths_to_delete):
            try:
                # 1. 删除图片文件
                if os.path.exists(img_path):
                    os.remove(img_path)
                
                # 2. 尝试删除所有可能的标签文件 (json, txt, xml)
                base_path = os.path.splitext(img_path)[0]
                for ext in [".json", ".txt", ".xml"]:
                    label_path = base_path + ext
                    if os.path.exists(label_path):
                        os.remove(label_path)
                
                # 从列表中移除
                row = self.listFiles.row(item)
                self.listFiles.takeItem(row)
                
                deleted_count += 1
            except Exception as e:
                print(f"删除文件 {img_path} 失败: {e}")

        # 恢复信号
        self.listFiles.currentItemChanged.connect(self.on_file_selected)

        if current_deleted:
            self.statusBar.showMessage(f"已删除 {deleted_count} 个文件，当前预览已清除", 3000)
            # 如果列表还有文件，自动选中并加载第一项或当前项
            if self.listFiles.count() > 0:
                current_row = self.listFiles.currentRow()
                if current_row < 0:
                    current_row = 0
                item = self.listFiles.item(current_row)
                self.listFiles.setCurrentItem(item)
                # 手动触发一次加载
                self.on_file_selected(item, None)
        else:
            self.statusBar.showMessage(f"成功删除 {deleted_count} 个文件", 3000)

    def on_file_selected(self, current, previous):
        if previous:
            self.auto_save_annotation()

        if current:
            path = current.text()
            self.current_image_path = path
            self.scene.load_image(path)
            self.view.fitInView(self.scene.sceneRect(), Qt.KeepAspectRatio)
            self.load_annotations(path)

            self.undo_stack.clear()
            self.redo_stack.clear()
            self.push_state()
            
            # 清除旧图片的骨架预览
            if hasattr(self.scene, 'pose_preview_item') and self.scene.pose_preview_item:
                self.scene.removeItem(self.scene.pose_preview_item)
                self.scene.pose_preview_item = None

            if self.sam_client.model:
                # self.helpLabel.setText("正在分析图片智能特征...")
                # self.helpLabel.setStyleSheet("color: orange;")
                QApplication.processEvents()
                self.sam_client.set_image(path)
                # self.helpLabel.setText("分析完成，可以开始智能标注")
                # self.helpLabel.setStyleSheet("color: green;")
            else:
                self.helpLabel.setText("等待后台加载模型，稍后将自动分析图片...")
                self.helpLabel.setStyleSheet("color: orange;")

    def auto_save_annotation(self):
        if not self.current_image_path or not self.scene.img_item: return
        shapes_data = Exporter.extract_shapes(self.scene)
        # if not shapes_data: return

        img_rect = self.scene.img_item.pixmap().rect()
        base_name = os.path.splitext(self.current_image_path)[0]

        try:
            if self.current_format == "json":
                out_path = base_name + ".json"
                Exporter.save_json(out_path, self.current_image_path, img_rect.width(), img_rect.height(), shapes_data)
            elif self.current_format == "yolo":
                out_path = base_name + ".txt"
                Exporter.save_yolo(out_path, img_rect.width(), img_rect.height(), shapes_data, self.class_list)
            elif self.current_format == "xml":
                out_path = base_name + ".xml"
                Exporter.save_xml(out_path, self.current_image_path, img_rect.width(), img_rect.height(), shapes_data)
        except Exception as e:
            print(f"自动保存失败: {str(e)}")

    def set_current_format(self, format_type):
        self.current_format = format_type
        self.formatWidget.set_format(format_type)

        # if format_type == "json":
        #     self.actionFormatJSON.setChecked(True)
        # elif format_type == "yolo":
        #     self.actionFormatYOLO.setChecked(True)
        # elif format_type == "xml":
        #     self.actionFormatXML.setChecked(True)

        if self.current_image_path:
            self.scene.clear_shapes()
            self.load_annotations(self.current_image_path)
        DialogOver(self, f"当前保存及读取格式变为 {format_type.upper()}", "格式切换", "info")

    def load_annotations(self, image_path):
        if not self.scene.img_item: return

        img_w = self.scene.img_item.pixmap().width()
        img_h = self.scene.img_item.pixmap().height()
        base_path = os.path.splitext(image_path)[0]

        if self.current_format == "json":
            self._load_json(base_path + ".json")
        elif self.current_format == "yolo":
            self._load_yolo(base_path + ".txt", img_w, img_h)
        elif self.current_format == "xml":
            self._load_xml(base_path + ".xml")

    def _add_shape_to_scene(self, shape, label):
        """往画板内添加加载出来的轮廓并同步历史类别"""
        if label not in self.class_list:
            # self.class_list.append(label)
            # self.listClasses.addItem(label)
            self.add_class_to_list(label)
            self.save_classes()
        self.scene.addItem(shape)

    def _load_json(self, json_path):
        if not os.path.exists(json_path): return
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for shape_data in data.get("shapes", []):
                label = shape_data.get("label", "")
                points = shape_data.get("points", [])
                shape_type = shape_data.get("shape_type", "rectangle")

                if shape_type == "rectangle" and len(points) == 2:
                    rect = QRectF(points[0][0], points[0][1], points[1][0] - points[0][0], points[1][1] - points[0][1])
                    shape = RectShape(rect, label)
                elif shape_type == "polygon" and len(points) >= 3:
                    qpoints = [QPointF(p[0], p[1]) for p in points]
                    shape = PolyShape(QPolygonF(qpoints), label)
                elif shape_type == "point" and len(points) == 1:
                    shape = PointShape(QPointF(points[0][0], points[0][1]), label)
                # 旋转框 (OBB) 的解析分支
                elif shape_type == "obb":
                    rect_data = shape_data.get("rect")
                    angle = shape_data.get("angle", 0)
                    if rect_data and len(rect_data) == 4:
                        cx, cy, w, h = rect_data[0], rect_data[1], rect_data[2], rect_data[3]
                        shape = RotatedRectShape(cx, cy, w, h, angle, label)
                    else:
                        continue
                elif shape_type == "pose":
                    rect_data = shape_data.get("rect")
                    template_name = shape_data.get("template_name", "")
                    kps_data = shape_data.get("keypoints", [])
                    if rect_data and len(rect_data) == 4:
                        cx, cy, w, h = rect_data
                        angle = shape_data.get("angle", 0)
                        template = self.template_manager.get_template(template_name)
                        if not template:
                            # 降级处理，或者用空模板
                            template = {"name": template_name, "keypoints": [], "connections": []}
                            for i, kp in enumerate(kps_data):
                                template["keypoints"].append({"name": f"kp_{i}", "color": "#00FF00", "default_pos": [0.5, 0.5]})
                        
                        shape = PoseShape(QRectF(cx - w / 2, cy - h / 2, w, h), template, label)
                        shape.setRotation(angle)
                        # 设置读取的关键点位置和可见性
                        for i, kp_data in enumerate(kps_data):
                            if i < len(shape.kps):
                                local_pt = shape.mapFromScene(QPointF(kp_data[0], kp_data[1]))
                                shape.kps[i].setPos(local_pt)
                                shape.kps[i].set_visibility(kp_data[2])
                        shape.update_lines()
                    else:
                        continue
                else:
                    continue
                self._add_shape_to_scene(shape, label)
        except Exception as e:
            print(f"加载 JSON 标注失败: {e}")

    def _load_yolo(self, txt_path, img_w, img_h):
        if not os.path.exists(txt_path): return
        import math  # 局部导入数学库，用于逆向推导
        try:
            with open(txt_path, "r", encoding="utf-8") as f:
                lines = f.readlines()

            for line in lines:
                parts = line.strip().split()
                if not parts: continue
                class_id = int(parts[0])
                label = self.class_list[class_id] if class_id < len(self.class_list) else str(class_id)
                shape = None

                # 1. YOLO BBox 格式 (常规矩形：5个参数)
                if len(parts) == 5:
                    cx, cy = float(parts[1]) * img_w, float(parts[2]) * img_h
                    w, h = float(parts[3]) * img_w, float(parts[4]) * img_h
                    shape = RectShape(QRectF(cx - w / 2, cy - h / 2, w, h), label)

                # YOLO OBB 旋转框格式 (9个参数：1 个类别 + 8 个坐标)
                elif len(parts) == 9:
                    x1, y1 = float(parts[1]) * img_w, float(parts[2]) * img_h
                    x2, y2 = float(parts[3]) * img_w, float(parts[4]) * img_h
                    x3, y3 = float(parts[5]) * img_w, float(parts[6]) * img_h
                    x4, y4 = float(parts[7]) * img_w, float(parts[8]) * img_h

                    # 利用四边形的顶点逆向推导出原生属性
                    cx = (x1 + x2 + x3 + x4) / 4.0
                    cy = (y1 + y2 + y3 + y4) / 4.0
                    w = math.hypot(x2 - x1, y2 - y1)
                    h = math.hypot(x4 - x1, y4 - y1)
                    angle = math.degrees(math.atan2(y2 - y1, x2 - x1))

                    # 重新生成带有完美手柄的 OBB 对象
                    shape = RotatedRectShape(cx, cy, w, h, angle, label)

                # YOLO Pose / Polygon 格式冲突处理
                elif len(parts) > 5:
                    # 尝试解析为 Pose
                    is_pose = False
                    kp_parts = parts[5:]
                    has_vis = (len(kp_parts) % 3 == 0)
                    kp_count = len(kp_parts) // 3 if has_vis else len(kp_parts) // 2
                    
                    template = self.scene.current_pose_template
                    if template and len(template.get("keypoints", [])) == kp_count:
                        is_pose = True
                    elif has_vis and len(parts) % 2 == 0: # 通常 Polygon 长度为奇数，如果有 visibility 导致长度为偶数且符合 3N，则肯定是 Pose
                        is_pose = True
                        
                    if is_pose:
                        cx, cy = float(parts[1]) * img_w, float(parts[2]) * img_h
                        w, h = float(parts[3]) * img_w, float(parts[4]) * img_h
                        if not template or len(template.get("keypoints", [])) != kp_count:
                            template = {"name": "YOLO_Import", "keypoints": [], "connections": []}
                            for i in range(kp_count):
                                template["keypoints"].append({"name": f"kp_{i}", "color": "#00FF00", "default_pos": [0.5, 0.5]})
                        
                        shape = PoseShape(QRectF(cx - w / 2, cy - h / 2, w, h), template, label)
                        idx = 0
                        for i in range(kp_count):
                            kx = float(kp_parts[idx]) * img_w
                            ky = float(kp_parts[idx+1]) * img_h
                            vis = int(float(kp_parts[idx+2])) if has_vis else 2
                            idx += 3 if has_vis else 2
                            if i < len(shape.kps):
                                local_pt = shape.mapFromScene(QPointF(kx, ky))
                                shape.kps[i].setPos(local_pt)
                                shape.kps[i].set_visibility(vis)
                        shape.update_lines()
                        self._add_shape_to_scene(shape, label)
                        continue

                    # 如果不是 Pose，且满足多边形条件
                    elif len(parts) % 2 == 1:
                        qpoints = [QPointF(float(parts[i]) * img_w, float(parts[i + 1]) * img_h) for i in
                                   range(1, len(parts), 2)]
                        shape = PolyShape(QPolygonF(qpoints), label)

                if shape is not None:
                    self._add_shape_to_scene(shape, label)
        except Exception as e:
            print(f"加载 YOLO 标注失败: {e}")

    def _load_xml(self, xml_path):
        import xml.etree.ElementTree as ET
        if not os.path.exists(xml_path): return
        try:
            tree = ET.parse(xml_path)
            root = tree.getroot()
            for obj in root.findall("object"):
                label = obj.find("name").text
                bndbox = obj.find("bndbox")
                if bndbox is not None:
                    xmin, ymin = float(bndbox.find("xmin").text), float(bndbox.find("ymin").text)
                    xmax, ymax = float(bndbox.find("xmax").text), float(bndbox.find("ymax").text)
                    shape = RectShape(QRectF(xmin, ymin, xmax - xmin, ymax - ymin), label)
                    self._add_shape_to_scene(shape, label)
        except Exception as e:
            print(f"加载 XML 标注失败: {e}")

    def save_annotation(self, format_type):
        if not self.current_image_path or not self.scene.img_item:
            QMessageBox.warning(self, "提示", "请先打开图片")
            DialogOver(self, "请先在左侧树形目录中打开图片", "操作错误", "warning")
            return
        shapes_data = Exporter.extract_shapes(self.scene)
        # if not shapes_data:
        #     DialogOver(self, "当前画布没有标注内容可保存", "为空提示", "warning")
        #     return

        img_rect = self.scene.img_item.pixmap().rect()
        base_name = os.path.splitext(self.current_image_path)[0]

        try:
            if format_type == "json":
                out_path = base_name + ".json"
                Exporter.save_json(out_path, self.current_image_path, img_rect.width(), img_rect.height(), shapes_data)
            elif format_type == "yolo":
                out_path = base_name + ".txt"
                Exporter.save_yolo(out_path, img_rect.width(), img_rect.height(), shapes_data, self.class_list)
            elif format_type == "xml":
                out_path = base_name + ".xml"
                Exporter.save_xml(out_path, self.current_image_path, img_rect.width(), img_rect.height(), shapes_data)

            DialogOver(self, f"标注文件保存/更新成功！", "保存成功", "success")
            print(f"标注文件已保存到: {out_path}")
        except Exception as e:
            DialogOver(self, f"写入失败: {str(e)}", "保存出错", "danger")

    def closeEvent(self, event):
        self.auto_save_annotation()
        self.sam_client.cleanup()
        super().closeEvent(event)

    def keyPressEvent(self, event):
        key = event.key()
        modifiers = event.modifiers()

        # 撤销与重做快捷键拦截
        if key == Qt.Key_Z and modifiers == Qt.ControlModifier:
            # Shift + Ctrl + Z 或者是多边形画点撤回处理
            if modifiers & Qt.ShiftModifier:
                self.redo()
            elif self.scene.mode == CanvasMode.POLY and len(self.scene.poly_pts) > 0:
                pass  # 多边形绘制中的撤销点由 canvas 自己处理，主窗口跳过
            else:
                self.undo()
        elif key == Qt.Key_Y and modifiers == Qt.ControlModifier:
            self.redo()

        if key == Qt.Key_D or key == Qt.Key_Right:
            current_idx = self.listFiles.currentRow()
            if current_idx < self.listFiles.count() - 1:
                self.listFiles.setCurrentRow(current_idx + 1)
        elif key == Qt.Key_A or key == Qt.Key_Left:
            current_idx = self.listFiles.currentRow()
            if current_idx > 0:
                self.listFiles.setCurrentRow(current_idx - 1)
        elif key == Qt.Key_Return or key == Qt.Key_Enter or key == Qt.Key_Escape:
            # 取消所有选中状态 (退出编辑模式)
            for item in self.scene.selectedItems():
                item.setSelected(False)
        elif key == Qt.Key_S and modifiers == Qt.ControlModifier:
            self.save_annotation(self.current_format)
        elif key == Qt.Key_E:  # 快捷键 E 修改类别
            selected_items = self.scene.selectedItems()
            for item in selected_items:
                if hasattr(item, 'label'):
                    self.edit_shape_label(item)
                    break

        elif key == Qt.Key_Q:
            self.samSwitch.setChecked(not self.samSwitch.isChecked())
        elif key == Qt.Key_F1:
            self.show_help_dialog()
        elif key == Qt.Key_R:
            self.actionRect.trigger()
        elif key == Qt.Key_P:
            self.actionPoly.trigger()
        elif key == Qt.Key_M:
            # M for Model Prediction in Smart mode
            if self.samSwitch.isChecked() and getattr(self, 'current_yolo_predictor', None) is not None:
                self.btnPredict.click()
        elif key == Qt.Key_T:
            self.actionPoint.trigger()
        elif key == Qt.Key_O:
            self.actionRBox.trigger()

        super().keyPressEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())