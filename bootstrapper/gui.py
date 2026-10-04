"""Small graphical installer; no terminal or developer environment on the user's machine."""
from __future__ import annotations

import json
import queue
import re
import sys
import threading
import tkinter as tk
import uuid
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from bootstrapper import core


class Installer(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("RDOS Runner 接入")
        self.geometry("640x470")
        self.minsize(560, 420)
        self.code = tk.StringVar()
        self.workspace = tk.StringVar()
        self.node = tk.StringVar()
        self.message = tk.StringVar(value="请粘贴管理员交付的一次性接入码。")
        self.busy = False
        self.updates = queue.Queue()
        body = ttk.Frame(self, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="RDOS Runner", font=("Helvetica", 19, "bold")).pack(anchor="w")
        ttk.Label(body, text="安装后独立运行；WorkBuddy 只需读取 Workspace。", wraplength=580).pack(anchor="w", pady=(3, 16))
        ttk.Label(body, text="一次性接入码").pack(anchor="w")
        ttk.Entry(body, textvariable=self.code, show="•").pack(fill="x", pady=(3, 9))
        ttk.Label(body, text="Workspace（可选，默认当前用户的 RDOS-Workspace）").pack(anchor="w")
        location = ttk.Frame(body)
        location.pack(fill="x", pady=(3, 9))
        ttk.Entry(location, textvariable=self.workspace).pack(side="left", fill="x", expand=True)
        ttk.Button(location, text="选择目录", command=self.choose).pack(side="left", padx=(8, 0))
        self.install_button = ttk.Button(body, text="检查并安装", command=self.begin_install)
        self.install_button.pack(anchor="w", pady=(5, 16))
        ttk.Separator(body).pack(fill="x", pady=(0, 10))
        ttk.Label(body, text="已安装节点 ID").pack(anchor="w")
        self.node_picker = ttk.Combobox(body, textvariable=self.node, values=core.installed_nodes())
        self.node_picker.pack(fill="x", pady=(3, 6))
        if self.node_picker["values"]:
            self.node.set(self.node_picker["values"][0])
        actions = ttk.Frame(body)
        actions.pack(anchor="w", pady=(2, 10))
        for label, fn in (("状态", self.show_status), ("重新自测", self.retest),
                          ("启动", self.start), ("停止", self.stop), ("卸载", self.remove)):
            ttk.Button(actions, text=label, command=fn).pack(side="left", padx=(0, 7))
        ttk.Label(body, textvariable=self.message, wraplength=580).pack(anchor="w", pady=(12, 6))
        self.detail = tk.Text(body, height=6, state="disabled", wrap="word")
        self.detail.pack(fill="both", expand=True)
        ttk.Button(body, text="导出脱敏诊断", command=self.export_diagnostics).pack(anchor="e", pady=(8, 0))
        self.after(100, self._drain_updates)

    def choose(self):
        selected = filedialog.askdirectory(title="选择本机专用 Workspace")
        if selected:
            self.workspace.set(selected)

    def log(self, line: str):
        safe = re.sub(r"brt_[A-Za-z0-9_-]+|rnr_[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[已脱敏]", line)
        self.updates.put(safe)

    def _drain_updates(self):
        while not self.updates.empty():
            line = self.updates.get_nowait()
            if line is None:
                self._ready()
            elif isinstance(line, tuple) and line[0] == "installed":
                self.code.set("")
                self._append("接入完成；后台 Runner 已运行并通过同步、诊断 ACK 自测。")
            else:
                self._append(line)
        self.after(100, self._drain_updates)

    def _append(self, line: str):
        self.message.set(line)
        self.detail.configure(state="normal")
        self.detail.insert("end", line + "\n")
        self.detail.see("end")
        self.detail.configure(state="disabled")

    def work(self, fn):
        if self.busy:
            return
        self.busy = True
        self.install_button.configure(state="disabled")
        def invoke():
            try:
                result = fn()
                if isinstance(result, dict):
                    self.updates.put(("installed", result))
                else:
                    self.log(str(result))
            except Exception as exc:
                self.log("未完成：" + str(exc))
            finally:
                self.updates.put(None)
        threading.Thread(target=invoke, daemon=True).start()

    def _ready(self):
        self.busy = False
        self.install_button.configure(state="normal")
        self.node_picker.configure(values=core.installed_nodes())

    def begin_install(self):
        code = self.code.get().strip()
        try:
            node = core.node_from_code(code)
        except ValueError as exc:
            messagebox.showerror("接入码无效", str(exc))
            return
        self.node.set(node)
        workspace = Path(self.workspace.get()) if self.workspace.get().strip() else None
        self.work(lambda: core.install(code, workspace, self.log))

    def selected_node(self) -> str | None:
        node = self.node.get().strip()
        if not core.RUNNER_ID.fullmatch(node):
            messagebox.showinfo("选择节点", "请先粘贴接入码，或填写已安装节点 ID。")
            return None
        return node

    def show_status(self):
        node = self.selected_node()
        if node:
            self.work(lambda: json.dumps(core.status(node), ensure_ascii=False))

    def retest(self):
        node = self.selected_node()
        if not node:
            return
        def action():
            root = core.installation_root(node)
            config = json.loads((root / "config.json").read_text(encoding="utf-8"))
            event_id = str(uuid.uuid4())
            core.private_write(root / "self-test.json", {"event_id": event_id})
            core._wait_self_test(config, root, event_id, self.log)
            return "重新自测通过。"
        self.work(action)

    def start(self):
        node = self.selected_node()
        if node:
            self.work(lambda: (core.start_service(node), "后台 Runner 已启动。")[1])

    def stop(self):
        node = self.selected_node()
        if node:
            self.work(lambda: (core.stop_service(node), "后台 Runner 已停止。")[1])

    def remove(self):
        node = self.selected_node()
        if node and messagebox.askyesno("卸载", "卸载本机 Runner？Workspace 会保留，云端 Token 不会自动撤销。"):
            self.work(lambda: core.uninstall(node))

    def export_diagnostics(self):
        target = filedialog.asksaveasfilename(defaultextension=".txt", title="导出脱敏诊断")
        if target:
            content = self.detail.get("1.0", "end")
            content = re.sub(r"brt_[A-Za-z0-9_-]+|rnr_[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[已脱敏]", content)
            Path(target).write_text(content, encoding="utf-8")
            self.log("诊断已导出；请先检查内容，再分享给管理员。")


def main():
    if "--manager" in sys.argv:
        sys.argv.remove("--manager")
        from bootstrapper.manager import main as manager_main
        manager_main()
    else:
        Installer().mainloop()


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        core.platform_key()
    else:
        main()
