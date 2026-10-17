"""中文图形入口；预览和执行均复用现有核心，导入本模块不会打开窗口。"""

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, simpledialog, ttk

from operation_history import HistoryError
from rename_tool import CONFIRM_TEXT, ExecutionError, RenameError, execute_plan, make_plan


class ConfirmDialog(simpledialog.Dialog):
    """使用公开的 Dialog 基类，避免默认确认框出现英文按钮。"""

    def __init__(self, parent, prompt):
        self.prompt = prompt
        super().__init__(parent, title="确认重命名")

    def body(self, master):
        ttk.Label(master, text=self.prompt, wraplength=600, justify="left").pack(padx=12, pady=12)
        self.answer = ttk.Entry(master, width=35)
        self.answer.pack(padx=12, pady=(0, 12))
        return self.answer

    def buttonbox(self):
        buttons = ttk.Frame(self)
        ttk.Button(buttons, text="确认执行", command=self.ok).pack(side="left", padx=8)
        ttk.Button(buttons, text="取消", command=self.cancel).pack(side="left", padx=8)
        buttons.pack(pady=(0, 12))
        self.bind("<Return>", self.ok)
        self.bind("<Escape>", self.cancel)

    def apply(self):
        self.result = self.answer.get()


def ask_confirmation(prompt, parent):
    return ConfirmDialog(parent, prompt).result


class RenameWindow(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=12)
        self.pack(fill="both", expand=True)
        self.plan = ()
        self.preview_settings = None
        self.busy = False
        self.folder = tk.StringVar(master)
        self.extension = tk.StringVar(master)
        self.status = tk.StringVar(master, value="请先选择测试文件夹，再生成预览。")
        self.columnconfigure(1, weight=1)
        self.rowconfigure(4, weight=1)
        ttk.Label(self, text="目标文件夹：").grid(row=0, column=0, sticky="w")
        self.folder_entry = ttk.Entry(self, textvariable=self.folder, width=55)
        self.folder_entry.grid(row=0, column=1, sticky="ew", padx=6)
        self.choose_button = ttk.Button(self, text="选择文件夹", command=self.choose_folder)
        self.choose_button.grid(row=0, column=2)
        ttk.Label(self, text="文件扩展名：").grid(row=1, column=0, sticky="w", pady=8)
        self.extension_entry = ttk.Entry(self, textvariable=self.extension, width=16)
        self.extension_entry.grid(row=1, column=1, sticky="w", padx=6)
        ttk.Label(self, text="例如 .txt；留空处理所有普通文件，不处理子文件夹。限制沿用命令行版。",
                  wraplength=690).grid(row=2, column=0, columnspan=3, sticky="w")
        ttk.Label(self, text="新名字沿用默认规则：学习文件001、学习文件002……，保留扩展名。",
                  wraplength=690).grid(row=3, column=0, columnspan=3, sticky="w", pady=6)
        table = ttk.Frame(self)
        table.grid(row=4, column=0, columnspan=3, sticky="nsew")
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(table, columns=("old", "new", "result"), show="headings", height=12)
        for column, title, width in (("old", "原文件名", 260), ("new", "新文件名", 260), ("result", "状态", 120)):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, minwidth=100)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(table, orient="horizontal", command=self.tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        buttons = ttk.Frame(self)
        buttons.grid(row=5, column=0, columnspan=3, sticky="w", pady=8)
        self.preview_button = ttk.Button(buttons, text="生成预览", command=self.preview)
        self.preview_button.pack(side="left")
        self.execute_button = ttk.Button(buttons, text="确认并执行", command=self.apply, state="disabled")
        self.execute_button.pack(side="left", padx=12)
        ttk.Label(self, textvariable=self.status, wraplength=730).grid(row=6, column=0, columnspan=3, sticky="w")
        self.details = tk.Text(self, height=7, wrap="word", state="disabled")
        self.details.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.folder.trace_add("write", self.invalidate)
        self.extension.trace_add("write", self.invalidate)

    def settings(self):
        return self.folder.get().strip(), self.extension.get().strip()

    def write_details(self, text):
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", text)
        self.details.configure(state="disabled")

    def invalidate(self, *_):
        self.plan = ()
        self.preview_settings = None
        self.execute_button.configure(state="disabled")
        self.tree.delete(*self.tree.get_children())
        self.write_details("")
        self.status.set("设置已变化，请重新生成预览；当前不会执行改名。")

    def set_busy(self, value):
        self.busy = value
        for control in (self.folder_entry, self.extension_entry, self.choose_button, self.preview_button):
            control.configure(state="disabled" if value else "normal")
        self.execute_button.configure(state="normal" if not value and self.plan else "disabled")
        # 不调用 update() 或启动后台任务，避免复核期间重入另一项操作。
        self.update_idletasks()

    def choose_folder(self):
        if self.busy:
            return
        selected = filedialog.askdirectory(parent=self.winfo_toplevel(), title="选择要预览的测试文件夹", mustexist=True)
        if selected:
            self.folder.set(selected)

    def preview(self):
        if self.busy:
            return
        self.invalidate()
        folder, extension = self.settings()
        if not folder:
            self.status.set("请选择目标文件夹。")
            return
        self.set_busy(True)
        try:
            plan = tuple(make_plan(Path(folder), extension=extension or None))
            for position, item in enumerate(plan):
                state = "无需修改" if item.source.name == item.target.name else "未执行"
                self.tree.insert("", "end", iid=str(position), values=(item.source.name, item.target.name, state))
            count = sum(item.source.name != item.target.name for item in plan)
            if count:
                self.plan = plan
                self.preview_settings = self.settings()
            self.status.set(f"预览完成：匹配 {len(plan)} 个文件，需要改名 {count} 个；尚未修改文件或写入账本。")
        except (OSError, RenameError, HistoryError) as error:
            self.status.set("预览失败，未执行改名。")
            self.write_details(str(error))
        except Exception as error:
            self.status.set("预览遇到意外错误，未执行改名。")
            self.write_details(f"{type(error).__name__}：{error}")
        finally:
            self.set_busy(False)

    def show_results(self, plan, completed, current=None):
        successful = {item.source for item in completed}
        marked = False
        for position, item in enumerate(plan):
            if item.source.name == item.target.name:
                state = "无需修改"
            elif item.source in successful:
                state = "成功（已记录）"
            elif current and not marked:
                state, marked = current, True
            else:
                state = "未执行"
            self.tree.set(str(position), "result", state)

    def apply(self):
        if self.busy:
            return
        if not self.plan:
            # 执行后的结果表和异常详情保留，重复调用不能清空核查线索。
            if not self.tree.get_children():
                self.status.set("没有有效预览，请先重新生成预览。")
            return
        if self.preview_settings != self.settings():
            self.invalidate()
            self.status.set("没有有效预览，请先重新生成预览。")
            return
        plan, settings = self.plan, self.preview_settings
        count = sum(item.source.name != item.target.name for item in plan)
        answer = ask_confirmation(
            f"操作目录：{plan[0].source.parent}\n将按表格改名 {count} 个文件，并保存操作记录。\n"
            f"请核对预览后输入“{CONFIRM_TEXT}”；取消或其他文字均不执行。",
            parent=self.winfo_toplevel())
        if answer != CONFIRM_TEXT:
            self.status.set("已取消，未执行改名。")
            return
        # 对话框关闭后再次检查设置与计划，不能用旧预览处理新目标。
        if self.plan != plan or self.preview_settings != settings or self.settings() != settings:
            self.invalidate()
            self.status.set("预览已失效，请重新生成预览。")
            return
        self.plan = ()
        self.preview_settings = None
        self.set_busy(True)
        try:
            completed = execute_plan(plan, answer)
            self.show_results(plan, completed)
            self.status.set(f"执行完成：成功改名并记录 {len(completed)} 个文件。请重新预览后再操作。")
            self.write_details("操作记录已保存。第一版界面暂不提供历史与撤销；可通过原命令行查看历史。")
        except ExecutionError as error:
            current = "待核查" if error.needs_review else "失败"
            self.show_results(plan, error.completed, current)
            self.status.set(f"已停止：{current}；已持久确认成功 {len(error.completed)} 个。")
            self.write_details(f"操作编号：{error.operation_id}\n{error}\n不会自动重试、回滚或继续；请人工核查。")
        except (OSError, RenameError, HistoryError) as error:
            self.show_results(plan, [])
            self.status.set("执行被拒绝或无法启动；请查看原因并重新预览。")
            self.write_details(str(error))
        except Exception as error:
            self.show_results(plan, [], "待核查")
            self.status.set("发生意外错误，结果待核查，后续操作已停止。")
            self.write_details(f"{type(error).__name__}：{error}\n不要直接重试，请先人工核查文件和操作记录。")
        finally:
            self.set_busy(False)


def main():
    root = tk.Tk()
    root.title("批量文件重命名工具")
    root.geometry("820x600")
    root.minsize(700, 500)
    RenameWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
