"""Local, unrestricted desktop AI assistant backed by a local Ollama instance."""

import base64
import json
import os
import queue
import re
import threading
import time
import tkinter as tk
import uuid
from tkinter import colorchooser, filedialog, messagebox

import customtkinter as ctk
import requests

import chat_engine
import markdown_render
import telegram_bridge
from config import load_config, save_config
from db import (
    add_message,
    clear_session_messages,
    create_session,
    delete_session,
    init_db,
    list_sessions,
    load_messages,
    touch_session,
)
from ollama_client import OllamaClient
import tools

# Read once at import time: CTk's appearance mode and color theme are only
# safe to set before any window is created, and COLOR_* below are read by
# widget-construction code throughout this module — none of that can wait
# for an App instance to exist. Appearance mode can still be changed live
# afterward (ctk.set_appearance_mode); color theme and the COLOR_* accents
# take effect on next launch, same as any other CTk-theme-level setting.
_startup_cfg = load_config()

ctk.set_appearance_mode(_startup_cfg.appearance_mode)
ctk.set_default_color_theme(_startup_cfg.color_theme)

COLOR_OK = _startup_cfg.ok_color
COLOR_BAD = _startup_cfg.bad_color
COLOR_WARN = _startup_cfg.accent_color

RISK_DESCRIPTIONS = {
    "dev": "This can create, overwrite, or modify local files, or execute Python code.",
    "recon": "This will send network requests or run scanning tools against the target.",
    "security": "This can execute arbitrary shell commands or process local data.",
    "vision": "This takes a screenshot of your screen and shares it with the model.",
    "automation": "This sets up a recurring background action that will keep running "
    "without further confirmation until you cancel it.",
    "research": "This sends a query to the web and reads back results.",
    "memory": "This reads or writes to the assistant's persistent local memory store.",
    "report": "This saves a formatted report file locally.",
    "vuln": "This queries a public vulnerability database.",
    "hexstrike": "This runs a real security/pentesting tool (nmap, sqlmap, hydra, metasploit, etc.) "
    "via your HexStrike server against the specified target. Only approve this for systems "
    "you are explicitly authorized to test.",
    "general": "This will execute a local tool.",
}

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg")
ATTACHMENT_EXTENSIONS = (".txt", ".log", ".py", ".pcap", ".json", ".csv", ".pdf")


class ToolConfirmRequest:
    """Bridges a tool-call confirmation between the worker thread and the GUI thread."""

    def __init__(self, name: str, args: dict, category: str = "general"):
        self.name = name
        self.args = args
        self.category = category
        self.event = threading.Event()
        self.approved = False


class App(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.cfg = load_config()
        init_db()

        self.client = OllamaClient(self.cfg.base_url)
        self.messages: list[dict] = []
        self.current_session_id: int | None = None
        self.current_model = self.cfg.last_model

        self.stream_queue: "queue.Queue[dict]" = queue.Queue()
        self.stop_flag = threading.Event()
        self.is_generating = False
        self.tool_execution_enabled = self.cfg.tool_execution_enabled
        tools.state.tool_execution_enabled = self.tool_execution_enabled
        tools.state.sandbox_enabled = self.cfg.sandbox_enabled
        tools.state.workspace_folder = self.cfg.workspace_folder
        tools.state.workspace_sandboxed = self.cfg.workspace_sandboxed
        tools.registry.set_disabled(self.cfg.disabled_tools)
        self.tools_manager_dlg: ctk.CTkToplevel | None = None
        self._assistant_header_inserted = False
        self._assistant_block_start = None
        self._assistant_block_text = ""
        self._raw_text_widget = None

        self.pending_image_path: str | None = None
        self.pending_file_path: str | None = None
        self._chat_images: list = []  # keeps PhotoImage refs alive so Tk doesn't GC them

        self.telegram_bridge_instance: telegram_bridge.TelegramBridge | None = None
        self.telegram_session_id: int | None = None
        self.telegram_messages: list[dict] = []
        self.telegram_lock = threading.Lock()
        self.telegram_pending_confirms: dict[str, tuple] = {}

        self.model_manager_dlg: ctk.CTkToplevel | None = None
        self.pull_in_progress = False
        self.pull_stop_flag = threading.Event()

        self.title("Local AI Assistant")
        self.geometry(self.cfg.window_geometry)
        self.minsize(960, 640)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build_ui()
        self._ensure_session()
        self._refresh_models_async()
        self._poll_connection_loop()
        tools.task_log.add_listener(self._on_task_log_line)
        if self.cfg.telegram_enabled:
            self._start_telegram_bridge()
        self.after(30, self._poll_stream_queue)
        self._context_label_loop()

    def _context_label_loop(self):
        try:
            self._update_context_label()
        except Exception:  # noqa: BLE001 - purely cosmetic
            pass
        self.after(1500, self._context_label_loop)

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.sidebar = ctk.CTkFrame(self, width=300)
        self.sidebar.grid(row=0, column=0, sticky="nsw")
        self.sidebar.grid_propagate(False)

        self.main_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.main_frame.grid(row=0, column=1, sticky="nsew")
        self.main_frame.grid_rowconfigure(1, weight=1)
        self.main_frame.grid_columnconfigure(0, weight=1)

        self._build_sidebar()
        self._build_topbar()
        self._build_chat_area()
        self._build_input_area()

    def _build_sidebar(self):
        tabs = ctk.CTkTabview(self.sidebar)
        tabs.pack(fill="both", expand=True, padx=8, pady=8)
        tab_sessions = tabs.add("Sessions")
        tab_prompt = tabs.add("System Prompt")
        tab_settings = tabs.add("Settings")
        tab_history = tabs.add("Task History")
        tab_help = tabs.add("Help")

        # --- Sessions tab
        btn_frame = ctk.CTkFrame(tab_sessions, fg_color="transparent")
        btn_frame.pack(fill="x", pady=(0, 6))
        ctk.CTkButton(btn_frame, text="New Session", command=self.new_session).pack(
            side="left", expand=True, fill="x", padx=2
        )
        ctk.CTkButton(
            btn_frame,
            text="Delete",
            fg_color=COLOR_BAD,
            hover_color="#8A2E2C",
            command=self.delete_current_session,
        ).pack(side="left", expand=True, fill="x", padx=2)

        self.sessions_list_frame = ctk.CTkScrollableFrame(tab_sessions, label_text="")
        self.sessions_list_frame.pack(fill="both", expand=True)

        ctk.CTkButton(tab_sessions, text="Clear Chat Log", command=self.clear_current_chat).pack(
            fill="x", pady=(6, 0)
        )

        # --- System prompt tab
        ctk.CTkLabel(tab_prompt, text="Custom System Prompt", anchor="w").pack(fill="x")
        self.system_prompt_box = ctk.CTkTextbox(tab_prompt, height=320, wrap="word")
        self.system_prompt_box.pack(fill="both", expand=True, pady=6)
        self.system_prompt_box.insert("1.0", self.cfg.system_prompt)
        ctk.CTkButton(tab_prompt, text="Save System Prompt", command=self.save_system_prompt).pack(fill="x")

        # --- Settings tab
        ctk.CTkLabel(tab_settings, text="Ollama Base URL", anchor="w").pack(fill="x", pady=(4, 0))
        self.base_url_entry = ctk.CTkEntry(tab_settings)
        self.base_url_entry.insert(0, self.cfg.base_url)
        self.base_url_entry.pack(fill="x", pady=(0, 6))
        ctk.CTkButton(tab_settings, text="Apply & Reconnect", command=self.apply_base_url).pack(fill="x")

        ctk.CTkLabel(tab_settings, text="Temperature", anchor="w").pack(fill="x", pady=(16, 0))
        temp_row = ctk.CTkFrame(tab_settings, fg_color="transparent")
        temp_row.pack(fill="x")
        self.temp_slider = ctk.CTkSlider(
            temp_row, from_=0.0, to=1.5, number_of_steps=30, command=self.on_temp_change
        )
        self.temp_slider.set(self.cfg.temperature)
        self.temp_slider.pack(side="left", fill="x", expand=True)
        self.temp_label = ctk.CTkLabel(temp_row, text=f"{self.cfg.temperature:.2f}", width=40)
        self.temp_label.pack(side="left", padx=(6, 0))

        ctk.CTkLabel(
            tab_settings, text="🎨 Appearance", text_color=COLOR_WARN, anchor="w"
        ).pack(fill="x", pady=(20, 0))

        ctk.CTkLabel(tab_settings, text="Appearance mode (live)", anchor="w", font=("", 11)).pack(
            fill="x", pady=(4, 0)
        )
        self.appearance_menu = ctk.CTkOptionMenu(
            tab_settings, values=["Dark", "Light", "System"], command=self.on_appearance_mode_change
        )
        self.appearance_menu.set(self.cfg.appearance_mode.capitalize())
        self.appearance_menu.pack(fill="x", pady=(2, 6))

        ctk.CTkLabel(
            tab_settings, text="Color theme (restart to apply)", anchor="w", font=("", 11)
        ).pack(fill="x")
        self.color_theme_menu = ctk.CTkOptionMenu(
            tab_settings, values=["Blue", "Green", "Dark-blue"], command=self.on_color_theme_change
        )
        self.color_theme_menu.set(self.cfg.color_theme.capitalize())
        self.color_theme_menu.pack(fill="x", pady=(2, 6))

        ctk.CTkLabel(tab_settings, text="Chat font (live)", anchor="w", font=("", 11)).pack(fill="x")
        font_row = ctk.CTkFrame(tab_settings, fg_color="transparent")
        font_row.pack(fill="x", pady=(2, 6))
        self.chat_font_menu = ctk.CTkOptionMenu(
            font_row,
            values=["Consolas", "Cascadia Code", "Courier New", "Segoe UI", "Arial"],
            command=self.on_chat_font_change,
            width=150,
        )
        self.chat_font_menu.set(self.cfg.chat_font_family)
        self.chat_font_menu.pack(side="left", padx=(0, 6))
        self.chat_font_size_slider = ctk.CTkSlider(
            font_row, from_=9, to=20, number_of_steps=11, command=self.on_chat_font_size_change
        )
        self.chat_font_size_slider.set(self.cfg.chat_font_size)
        self.chat_font_size_slider.pack(side="left", fill="x", expand=True)
        self.chat_font_size_label = ctk.CTkLabel(font_row, text=str(self.cfg.chat_font_size), width=24)
        self.chat_font_size_label.pack(side="left", padx=(4, 0))

        ctk.CTkLabel(
            tab_settings, text="Accent colors (restart to apply)", anchor="w", font=("", 11)
        ).pack(fill="x")
        color_row = ctk.CTkFrame(tab_settings, fg_color="transparent")
        color_row.pack(fill="x", pady=(2, 6))
        ctk.CTkButton(
            color_row, text="Accent", width=80, fg_color=self.cfg.accent_color,
            command=lambda: self.pick_color("accent_color"),
        ).pack(side="left", padx=(0, 4))
        ctk.CTkButton(
            color_row, text="OK", width=80, fg_color=self.cfg.ok_color,
            command=lambda: self.pick_color("ok_color"),
        ).pack(side="left", padx=(0, 4))
        ctk.CTkButton(
            color_row, text="Error", width=80, fg_color=self.cfg.bad_color,
            command=lambda: self.pick_color("bad_color"),
        ).pack(side="left")

        ctk.CTkLabel(
            tab_settings, text="⚠ Tool Execution", text_color=COLOR_WARN, anchor="w"
        ).pack(fill="x", pady=(20, 0))
        self.tool_switch_var = ctk.BooleanVar(value=self.tool_execution_enabled)
        self.tool_switch = ctk.CTkSwitch(
            tab_settings,
            text="Enable local tool execution",
            variable=self.tool_switch_var,
            command=self.on_tool_toggle,
        )
        self.tool_switch.pack(anchor="w", pady=6)
        ctk.CTkLabel(
            tab_settings,
            text=(
                "Allows the model to run shell commands, run\n"
                "Python code, and read/write local files.\n"
                "Every single call requires your confirmation."
            ),
            justify="left",
            text_color="gray60",
            font=("", 11),
            anchor="w",
        ).pack(fill="x")

        ctk.CTkLabel(
            tab_settings, text="📁 Workspace Folder", text_color=COLOR_WARN, anchor="w"
        ).pack(fill="x", pady=(20, 0))
        ctk.CTkLabel(
            tab_settings,
            text=(
                "Set a project folder ('📁 Folder' in the top bar) so relative\n"
                "paths and shell commands resolve inside it, and the model sees\n"
                "the file tree automatically."
            ),
            justify="left",
            text_color="gray60",
            font=("", 11),
            anchor="w",
        ).pack(fill="x", pady=(2, 4))
        recent_row = ctk.CTkFrame(tab_settings, fg_color="transparent")
        recent_row.pack(fill="x")
        recent_values = self.cfg.recent_workspaces or ["(none yet)"]
        self.recent_ws_menu = ctk.CTkOptionMenu(
            recent_row, values=recent_values, command=self.on_recent_workspace_selected, width=180
        )
        self.recent_ws_menu.set(recent_values[0])
        self.recent_ws_menu.pack(side="left", fill="x", expand=True, padx=(0, 6))
        ctk.CTkButton(recent_row, text="Clear", width=60, command=self.clear_workspace).pack(side="left")
        self.workspace_sandbox_var = ctk.BooleanVar(value=self.cfg.workspace_sandboxed)
        ctk.CTkSwitch(
            tab_settings,
            text="Confine tools to workspace (hard sandbox)",
            variable=self.workspace_sandbox_var,
            command=self.on_workspace_sandbox_toggle,
        ).pack(anchor="w", pady=6)

        ctk.CTkLabel(
            tab_settings, text="🐳 Docker Sandboxing", text_color=COLOR_WARN, anchor="w"
        ).pack(fill="x", pady=(20, 0))
        self.sandbox_switch_var = ctk.BooleanVar(value=self.cfg.sandbox_enabled)
        self.sandbox_switch = ctk.CTkSwitch(
            tab_settings,
            text="Sandbox shell/Python execution in Docker",
            variable=self.sandbox_switch_var,
            command=self.on_sandbox_toggle,
        )
        self.sandbox_switch.pack(anchor="w", pady=6)
        ctk.CTkLabel(
            tab_settings,
            text=(
                "Routes run_shell_command and run_python_script through a\n"
                "disposable Docker container instead of your host directly.\n"
                "Other tools (nmap, file access, etc.) are unaffected. Falls\n"
                "back to direct execution with a clear warning if Docker isn't\n"
                "installed or running."
            ),
            justify="left",
            text_color="gray60",
            font=("", 11),
            anchor="w",
        ).pack(fill="x")
        self.sandbox_status_label = ctk.CTkLabel(tab_settings, text="", text_color="gray60")
        self.sandbox_status_label.pack(anchor="w", pady=(4, 0))
        self._refresh_sandbox_status()

        ctk.CTkLabel(
            tab_settings, text="📱 Telegram Bot", text_color=COLOR_WARN, anchor="w"
        ).pack(fill="x", pady=(20, 0))
        ctk.CTkLabel(
            tab_settings,
            text=(
                "1. Message @BotFather on Telegram → /newbot → copy the token.\n"
                "2. Message @userinfobot to get your numeric user ID.\n"
                "3. Paste both below, then enable. Only your ID(s) can use the bot;\n"
                "everyone else is silently ignored."
            ),
            justify="left",
            text_color="gray60",
            font=("", 11),
            anchor="w",
        ).pack(fill="x", pady=(2, 6))

        self.telegram_token_entry = ctk.CTkEntry(tab_settings, placeholder_text="Bot token", show="*")
        self.telegram_token_entry.insert(0, self.cfg.telegram_bot_token)
        self.telegram_token_entry.pack(fill="x", pady=(0, 4))

        self.telegram_ids_entry = ctk.CTkEntry(
            tab_settings, placeholder_text="Allowed user ID(s), comma-separated"
        )
        self.telegram_ids_entry.insert(0, self.cfg.telegram_allowed_user_ids)
        self.telegram_ids_entry.pack(fill="x", pady=(0, 6))

        ctk.CTkButton(
            tab_settings, text="Save & Test Connection", command=self.save_and_test_telegram
        ).pack(fill="x")

        self.telegram_switch_var = ctk.BooleanVar(value=self.cfg.telegram_enabled)
        self.telegram_switch = ctk.CTkSwitch(
            tab_settings,
            text="Enable Telegram Bot",
            variable=self.telegram_switch_var,
            command=self.on_telegram_toggle,
        )
        self.telegram_switch.pack(anchor="w", pady=6)

        self.telegram_status_label = ctk.CTkLabel(tab_settings, text="Telegram: stopped", text_color="gray60")
        self.telegram_status_label.pack(anchor="w")

        ctk.CTkLabel(
            tab_settings, text="🔎 Web Search", text_color=COLOR_WARN, anchor="w"
        ).pack(fill="x", pady=(20, 0))
        ctk.CTkLabel(
            tab_settings,
            text=(
                "Optional. Without a key, web_search falls back to free DuckDuckGo\n"
                "scraping, which can get rate-limited under repeated use. A Brave\n"
                "Search API key (free, 2,000 queries/month, api.search.brave.com)\n"
                "makes it reliable."
            ),
            justify="left",
            text_color="gray60",
            font=("", 11),
            anchor="w",
        ).pack(fill="x", pady=(2, 6))
        self.brave_key_entry = ctk.CTkEntry(tab_settings, placeholder_text="Brave Search API key", show="*")
        self.brave_key_entry.insert(0, self.cfg.brave_api_key)
        self.brave_key_entry.pack(fill="x", pady=(0, 4))
        ctk.CTkButton(tab_settings, text="Save Key", command=self.save_brave_key).pack(fill="x")

        ctk.CTkLabel(
            tab_settings, text="🛡 CVE Lookup", text_color=COLOR_WARN, anchor="w"
        ).pack(fill="x", pady=(20, 0))
        ctk.CTkLabel(
            tab_settings,
            text="Optional. Works unauthenticated; a free NVD API key\n(nvd.nist.gov/developers/request-an-api-key) raises the rate limit.",
            justify="left",
            text_color="gray60",
            font=("", 11),
            anchor="w",
        ).pack(fill="x", pady=(2, 6))
        self.nvd_key_entry = ctk.CTkEntry(tab_settings, placeholder_text="NVD API key", show="*")
        self.nvd_key_entry.insert(0, self.cfg.nvd_api_key)
        self.nvd_key_entry.pack(fill="x", pady=(0, 4))
        ctk.CTkButton(tab_settings, text="Save Key", command=self.save_nvd_key).pack(fill="x")

        ctk.CTkLabel(
            tab_settings, text="🦂 HexStrike Integration", text_color=COLOR_WARN, anchor="w"
        ).pack(fill="x", pady=(20, 0))
        ctk.CTkLabel(
            tab_settings,
            text=(
                "For a locally/lab-hosted HexStrike AI server (e.g. a Parrot/Kali\n"
                "VM on your network) exposing 80+ pentesting tools. It has no\n"
                "built-in auth — only point this at a server on your own lab\n"
                "network, never the public internet."
            ),
            justify="left",
            text_color="gray60",
            font=("", 11),
            anchor="w",
        ).pack(fill="x", pady=(2, 6))
        self.hexstrike_url_entry = ctk.CTkEntry(
            tab_settings, placeholder_text="http://<parrot-vm-ip>:8888"
        )
        self.hexstrike_url_entry.insert(0, self.cfg.hexstrike_base_url)
        self.hexstrike_url_entry.pack(fill="x", pady=(0, 4))
        ctk.CTkButton(
            tab_settings, text="Save & Test Connection", command=self.save_and_test_hexstrike
        ).pack(fill="x")
        self.hexstrike_switch_var = ctk.BooleanVar(value=self.cfg.hexstrike_enabled)
        self.hexstrike_switch = ctk.CTkSwitch(
            tab_settings,
            text="Enable HexStrike tools",
            variable=self.hexstrike_switch_var,
            command=self.on_hexstrike_toggle,
        )
        self.hexstrike_switch.pack(anchor="w", pady=6)
        self.hexstrike_status_label = ctk.CTkLabel(tab_settings, text="HexStrike: not tested", text_color="gray60")
        self.hexstrike_status_label.pack(anchor="w")

        ctk.CTkLabel(
            tab_settings, text="🧩 Plugins", text_color=COLOR_WARN, anchor="w"
        ).pack(fill="x", pady=(20, 0))
        self.plugins_switch_var = ctk.BooleanVar(value=self.cfg.plugins_enabled)
        self.plugins_switch = ctk.CTkSwitch(
            tab_settings,
            text="Auto-load plugins on startup",
            variable=self.plugins_switch_var,
            command=self.on_plugins_toggle,
        )
        self.plugins_switch.pack(anchor="w", pady=6)
        plugin_btn_row = ctk.CTkFrame(tab_settings, fg_color="transparent")
        plugin_btn_row.pack(fill="x")
        ctk.CTkButton(plugin_btn_row, text="Open Plugins Folder", command=self.open_plugins_folder).pack(
            side="left", expand=True, fill="x", padx=(0, 2)
        )
        ctk.CTkButton(plugin_btn_row, text="Reload Plugins", command=self.reload_plugins_ui).pack(
            side="left", expand=True, fill="x", padx=(2, 0)
        )
        self.plugins_status_label = ctk.CTkLabel(
            tab_settings, text="", justify="left", wraplength=260, text_color="gray60", font=("", 11), anchor="w"
        )
        self.plugins_status_label.pack(fill="x", pady=(4, 0))
        self._refresh_plugins_status()

        self._build_registered_tools_list(tab_settings)

        # --- Task History tab
        ctk.CTkLabel(
            tab_history,
            text="Background task & watcher output — kept separate from chat.",
            text_color="gray60",
            font=("", 11),
            anchor="w",
        ).pack(fill="x", pady=(0, 6))
        self.task_log_box = ctk.CTkTextbox(tab_history, wrap="word", state="disabled", font=("Consolas", 11))
        self.task_log_box.pack(fill="both", expand=True, pady=(0, 6))
        for line in tools.task_log.read_recent():
            self._append_task_log_line(line)
        ctk.CTkButton(tab_history, text="Clear Task History", command=self.clear_task_history).pack(fill="x")

        # --- Help tab
        help_box = ctk.CTkTextbox(tab_help, wrap="word", font=("", 12))
        help_box.pack(fill="both", expand=True)
        help_box.insert("1.0", self._help_text())
        help_box.configure(state="disabled")

    def _help_text(self) -> str:
        return (
            "LOCAL AI ASSISTANT — QUICK GUIDE\n"
            "================================\n\n"
            "TOOLS: WHAT THEY ARE\n"
            "The model can call local 'tools' to do real work — read/write files, run\n"
            "shell commands, search the web, scan targets, and more. Tools only run when\n"
            "you enable them and approve each call.\n\n"
            "ENABLING TOOL USE\n"
            "1. Settings tab → turn ON 'Enable local tool execution'.\n"
            "2. When the model wants to run a tool, a confirmation dialog shows the tool\n"
            "   name, what it can do, and the exact arguments. Click Yes to allow, No to deny.\n"
            "3. This applies everywhere, including tools triggered from Telegram.\n\n"
            "FINDING TOOLS\n"
            "Click '🧰 Tools' in the top bar to open Manage Tools. It lists every tool\n"
            "grouped by category, with a description and its parameters. Use the filter box\n"
            "to search by name or description.\n\n"
            "ENABLING / DISABLING INDIVIDUAL TOOLS\n"
            "In Manage Tools, flip a tool's switch off to hide it from the model completely\n"
            "(e.g. disable run_shell_command but keep everything else). Your choices persist.\n\n"
            "ADDING YOUR OWN TOOLS (PLUGINS)\n"
            "Drop a .py file into the tools/plugins/ folder with a top-level\n"
            "register(registry) function. Example:\n\n"
            "    def my_tool(text: str) -> str:\n"
            "        return text.upper()\n\n"
            "    def register(registry):\n"
            "        registry.register(\n"
            "            'my_tool', 'Uppercase some text.',\n"
            "            {'type':'object','properties':{'text':{'type':'string'}},\n"
            "             'required':['text']},\n"
            "            my_tool, category='general')\n\n"
            "Then Settings → Plugins → 'Reload Plugins' (or restart). 'Open Plugins Folder'\n"
            "opens the directory for you.\n\n"
            "REMOVING A TOOL\n"
            "Built-in tool: disable it in Manage Tools. Plugin tool: delete its .py file\n"
            "from tools/plugins/ and reload.\n\n"
            "WORKING IN A PROJECT FOLDER\n"
            "Click '📁 Folder' in the top bar to set a workspace. Relative paths in file\n"
            "tools (read_file, write_file, list_directory, search_in_files) then resolve\n"
            "against that folder, and shell commands run inside it. The model is also given\n"
            "a file tree of the folder automatically. Turn on 'Confine tools to workspace'\n"
            "in Settings for a hard sandbox that blocks any path outside the folder.\n\n"
            "SEARCHING CODE\n"
            "The search_in_files tool greps recursively through a folder — great for finding\n"
            "where something is defined or used across a project.\n\n"
            "CONTEXT METER\n"
            "The '~Nk ctx' readout in the top bar estimates how full the conversation's\n"
            "context window is. Start a New Session when it gets large to keep responses sharp.\n\n"
            "SWITCHING MODELS\n"
            "Use the Model dropdown, or '⚙ Manage' to install/remove models. Models tagged\n"
            "with tool support work most reliably for tool use.\n\n"
            "OTHER SETTINGS (Settings tab)\n"
            "- Docker sandboxing for shell/Python execution\n"
            "- Telegram bot for phone access\n"
            "- Web search (Brave) and CVE lookup (NVD) API keys\n"
            "- HexStrike integration for lab pentesting tools\n"
            "- Appearance: theme, font, colors\n"
        )

    def _build_registered_tools_list(self, parent):
        specs = tools.registry.specs()
        ctk.CTkLabel(
            parent, text=f"Registered Tools ({len(specs)})", anchor="w", font=("", 12, "bold")
        ).pack(fill="x", pady=(18, 4))

        by_category: dict[str, list[str]] = {}
        for spec in specs:
            by_category.setdefault(spec.category, []).append(spec.name)

        for category in sorted(by_category):
            names = ", ".join(sorted(by_category[category]))
            ctk.CTkLabel(
                parent,
                text=f"{category}: {names}",
                justify="left",
                wraplength=260,
                text_color="gray60",
                font=("", 11),
                anchor="w",
            ).pack(fill="x", pady=(2, 0))

    def _build_topbar(self):
        bar = ctk.CTkFrame(self.main_frame, height=48)
        bar.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))

        ctk.CTkLabel(bar, text="Model:").pack(side="left", padx=(10, 4), pady=8)
        self.model_menu = ctk.CTkOptionMenu(
            bar, values=["(loading...)"], command=self.on_model_change, width=260
        )
        self.model_menu.pack(side="left", padx=4, pady=8)

        ctk.CTkButton(bar, text="⟳", width=32, command=self._refresh_models_async).pack(
            side="left", padx=4, pady=8
        )
        ctk.CTkButton(bar, text="⚙ Manage", width=90, command=self.open_model_manager).pack(
            side="left", padx=4, pady=8
        )
        ctk.CTkButton(bar, text="🧰 Tools", width=80, command=self.open_tools_manager).pack(
            side="left", padx=4, pady=8
        )
        ctk.CTkButton(bar, text="📁 Folder", width=90, command=self.add_folder).pack(
            side="left", padx=4, pady=8
        )

        self.conn_dot = ctk.CTkLabel(bar, text="●", text_color="gray50", font=("", 16))
        self.conn_dot.pack(side="left", padx=(16, 2), pady=8)
        self.conn_label = ctk.CTkLabel(bar, text="Checking connection...")
        self.conn_label.pack(side="left", pady=8)

        self.workspace_label = ctk.CTkLabel(bar, text="", text_color=COLOR_OK, font=("", 11))
        self.workspace_label.pack(side="left", padx=(12, 0), pady=8)

        self.tool_status_label = ctk.CTkLabel(bar, text="Tools: Idle", text_color="gray60")
        self.tool_status_label.pack(side="right", padx=12, pady=8)

        self.context_label = ctk.CTkLabel(bar, text="", text_color="gray60", font=("", 11))
        self.context_label.pack(side="right", padx=4, pady=8)

        self._update_workspace_label()

    def _build_chat_area(self):
        frame = ctk.CTkFrame(self.main_frame)
        frame.grid(row=1, column=0, sticky="nsew", padx=8, pady=4)
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)

        self.chat_box = ctk.CTkTextbox(
            frame, wrap="word", state="disabled", font=(self.cfg.chat_font_family, self.cfg.chat_font_size)
        )
        self.chat_box.grid(row=0, column=0, sticky="nsew")
        self._configure_chat_tags()
        if self._raw_text_widget is not None:
            self._raw_text_widget.bind("<Button-3>", self._show_chat_context_menu)

    def _build_input_area(self):
        frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        frame.grid(row=2, column=0, sticky="ew", padx=8, pady=(4, 8))
        frame.grid_columnconfigure(0, weight=1)

        attach_row = ctk.CTkFrame(frame, fg_color="transparent")
        attach_row.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        ctk.CTkButton(attach_row, text="🖼 Attach Image", width=130, command=self.attach_image).pack(
            side="left", padx=(0, 6)
        )
        ctk.CTkButton(attach_row, text="📄 Attach File", width=120, command=self.attach_file).pack(
            side="left", padx=(0, 6)
        )
        ctk.CTkButton(attach_row, text="📋 Copy Last Response", width=170, command=self.copy_last_response).pack(
            side="left"
        )

        self.attachment_bar = ctk.CTkFrame(frame, fg_color="transparent")
        self.attachment_bar.grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 4))

        self.input_box = ctk.CTkTextbox(frame, height=70, wrap="word")
        self.input_box.grid(row=2, column=0, sticky="ew", padx=(0, 8))
        self.input_box.bind("<Return>", self._on_enter)
        self.input_box.bind("<Shift-Return>", self._on_shift_enter)

        btns = ctk.CTkFrame(frame, fg_color="transparent")
        btns.grid(row=2, column=1, sticky="ns")
        self.send_btn = ctk.CTkButton(btns, text="Send", command=self.send_message)
        self.send_btn.pack(fill="x", pady=(0, 4))
        self.stop_btn = ctk.CTkButton(
            btns,
            text="Stop",
            fg_color=COLOR_BAD,
            hover_color="#8A2E2C",
            command=self.stop_generation,
            state="disabled",
        )
        self.stop_btn.pack(fill="x")

    # ---------------------------------------------------------- attachments

    def attach_image(self):
        path = filedialog.askopenfilename(
            title="Attach Image", filetypes=[("Images", "*.png *.jpg *.jpeg")]
        )
        if not path:
            return
        self.pending_image_path = path
        self._refresh_attachment_bar()

    def clear_image_attachment(self):
        self.pending_image_path = None
        self._refresh_attachment_bar()

    def attach_file(self):
        path = filedialog.askopenfilename(
            title="Attach File",
            filetypes=[
                ("Supported files", "*.txt *.log *.py *.pcap *.json *.csv *.pdf"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        self.pending_file_path = path
        self._refresh_attachment_bar()

    def clear_file_attachment(self):
        self.pending_file_path = None
        self._refresh_attachment_bar()

    def _refresh_attachment_bar(self):
        for w in self.attachment_bar.winfo_children():
            w.destroy()
        if self.pending_image_path:
            self._add_attachment_chip(
                os.path.basename(self.pending_image_path), self.clear_image_attachment, "🖼"
            )
        if self.pending_file_path:
            self._add_attachment_chip(
                os.path.basename(self.pending_file_path), self.clear_file_attachment, "📄"
            )

    def _add_attachment_chip(self, label_text: str, on_clear, icon: str):
        chip = ctk.CTkFrame(self.attachment_bar, fg_color="#2E5C8A", corner_radius=6)
        chip.pack(side="left", padx=(0, 6))
        ctk.CTkLabel(chip, text=f"{icon} {label_text}", text_color="white", font=("", 11)).pack(
            side="left", padx=(8, 4), pady=2
        )
        ctk.CTkButton(
            chip, text="✕", width=20, height=20, fg_color="transparent", hover_color="#3A6FA5", command=on_clear
        ).pack(side="left", padx=(0, 4), pady=2)

    def _insert_image_thumbnail(self, path: str, max_width: int = 320):
        try:
            from PIL import Image, ImageTk

            img = Image.open(path)
            img.thumbnail((max_width, max_width))
            photo = ImageTk.PhotoImage(img)
        except Exception:
            self._insert(f"[image: {os.path.basename(path)}]\n", "system")
            return
        self._chat_images.append(photo)
        self.chat_box.configure(state="normal")
        if self._raw_text_widget is not None:
            self._raw_text_widget.image_create("end", image=photo)
        self.chat_box.configure(state="disabled")
        self.chat_box.see("end")

    def _configure_chat_tags(self):
        """Applies chat font settings to all tags. Re-callable any time the
        font family/size setting changes — Tk re-renders already-tagged text
        immediately when a tag's font option changes, so this is how the
        "live" half of the Appearance settings actually takes effect."""
        text_widget = getattr(self.chat_box, "_textbox", None)
        self._raw_text_widget = text_widget
        if text_widget is None:
            return

        family = self.cfg.chat_font_family
        size = self.cfg.chat_font_size
        code_size = max(size - 1, 8)
        try:
            self.chat_box.configure(font=(family, size))
        except Exception:  # noqa: BLE001 - cosmetic only, never block on it
            pass

        text_widget.tag_config("user_header", foreground="#7FB2E5", font=("Segoe UI", 11, "bold"))
        text_widget.tag_config("user", foreground="#D8E6F3", font=(family, size))
        text_widget.tag_config("assistant_header", foreground="#7FD99A", font=("Segoe UI", 11, "bold"))
        text_widget.tag_config("assistant", foreground="#E8E8E8", font=(family, size))
        text_widget.tag_config("tool_header", foreground=COLOR_WARN, font=("Segoe UI", 11, "bold"))
        text_widget.tag_config("tool", foreground="#C9B37E", font=(family, size))
        text_widget.tag_config("system", foreground="#B06666", font=("Segoe UI", 10, "italic"))

        text_widget.tag_config(
            "md_code_block", foreground="#D8E6F3", background="#2A2A2A", font=(family, code_size)
        )
        text_widget.tag_config(
            "md_code_inline", foreground=COLOR_WARN, background="#2A2A2A", font=(family, code_size)
        )
        text_widget.tag_config("md_bold", foreground="#FFFFFF", font=("Segoe UI", size, "bold"))
        text_widget.tag_config("md_header1", foreground="#7FD99A", font=("Segoe UI", size + 3, "bold"))
        text_widget.tag_config("md_header2", foreground="#7FD99A", font=("Segoe UI", size + 1, "bold"))
        text_widget.tag_config("md_header3", foreground="#7FD99A", font=("Segoe UI", size, "bold"))
        text_widget.tag_config("md_bullet", foreground=COLOR_WARN)

    def _append_task_log_line(self, line: str):
        self.task_log_box.configure(state="normal")
        self.task_log_box.insert("end", line + "\n")
        self.task_log_box.configure(state="disabled")
        self.task_log_box.see("end")

    def _on_task_log_line(self, line: str):
        # Called from background scheduler threads — never touch Tk widgets
        # directly here, route through the same thread-safe queue as everything else.
        self.stream_queue.put({"type": "task_log_line", "line": line})

    def clear_task_history(self):
        tools.task_log.clear()
        self.task_log_box.configure(state="normal")
        self.task_log_box.delete("1.0", "end")
        self.task_log_box.configure(state="disabled")

    # ------------------------------------------------------------- helpers

    def _insert(self, text: str, tag: str | None = None):
        self.chat_box.configure(state="normal")
        if self._raw_text_widget is not None:
            self._raw_text_widget.insert("end", text, tag if tag else ())
        else:
            self.chat_box.insert("end", text)
        self.chat_box.configure(state="disabled")
        self.chat_box.see("end")

    def _clear_chat_display(self):
        self.chat_box.configure(state="normal")
        self.chat_box.delete("1.0", "end")
        self.chat_box.configure(state="disabled")
        self._assistant_header_inserted = False
        self._assistant_block_start = None
        self._assistant_block_text = ""

    def _append_chat_block(self, role: str, text: str):
        text = (text or "").strip()
        if role == "user":
            self._insert("You\n", "user_header")
            self._insert(text + "\n\n", "user")
        elif role == "assistant":
            if self._raw_text_widget is not None:
                self.chat_box.configure(state="normal")
                self._raw_text_widget.insert("end", "Assistant\n", ("assistant_header",))
                markdown_render.render_into(self._raw_text_widget, text)
                self._raw_text_widget.insert("end", "\n\n")
                self.chat_box.configure(state="disabled")
                self.chat_box.see("end")
            else:
                self._insert("Assistant\n", "assistant_header")
                self._insert(text + "\n\n", "assistant")
        elif role == "tool":
            self._insert(text + "\n\n", "tool")
        else:
            self._insert(f"[{role}] {text}\n\n", "system")

    def _append_assistant_token(self, text: str):
        if not self._assistant_header_inserted:
            self._assistant_block_start = self.chat_box.index("end-1c")
            self._assistant_block_text = ""
            self._insert("Assistant\n", "assistant_header")
            self._assistant_header_inserted = True
        self._insert(text, "assistant")
        self._assistant_block_text += text

    def _end_assistant_block(self):
        # Streaming stays raw token-by-token for responsiveness; once a block
        # is done, re-render it as one clean markdown pass rather than trying
        # to markdown-format partial/incomplete text mid-stream.
        #
        # Everything below runs in a single normal/disabled cycle — toggling
        # per-call (e.g. via _insert) would leave the widget disabled between
        # the header insert and the markdown render, and Tk silently no-ops
        # inserts into a disabled Text widget rather than raising.
        if self._assistant_header_inserted and self._assistant_block_start is not None:
            self.chat_box.configure(state="normal")
            body = self._assistant_block_text.strip()
            if self._raw_text_widget is not None:
                self._raw_text_widget.delete(self._assistant_block_start, "end")
                self._raw_text_widget.insert("end", "Assistant\n", ("assistant_header",))
                if body:
                    markdown_render.render_into(self._raw_text_widget, body)
                    self._raw_text_widget.insert("end", "\n")
                self._raw_text_widget.insert("end", "\n")
            else:
                self.chat_box.delete(self._assistant_block_start, "end")
                self.chat_box.insert("end", "Assistant\n")
                if body:
                    self.chat_box.insert("end", body + "\n")
                self.chat_box.insert("end", "\n")
            self.chat_box.configure(state="disabled")
            self.chat_box.see("end")
        self._assistant_header_inserted = False
        self._assistant_block_start = None
        self._assistant_block_text = ""

    def _append_tool_request_block(self, name: str, raw_args):
        if isinstance(raw_args, str):
            try:
                raw_args = json.loads(raw_args)
            except json.JSONDecodeError:
                pass
        pretty = json.dumps(raw_args, indent=2) if isinstance(raw_args, dict) else str(raw_args)
        self._insert(f"\U0001f527 Tool Call: {name}\n", "tool_header")
        self._insert(pretty + "\n\n", "tool")

    def _append_tool_result_block(self, name: str, result: str):
        self._insert(f"✅ Tool Result [{name}]\n", "tool_header")
        self._insert((result or "").strip() + "\n\n", "tool")

    # -------------------------------------------------------------- copy

    def _copy_to_clipboard(self, text: str):
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update()  # flush to the OS clipboard so it survives losing focus

    def copy_last_response(self):
        for msg in reversed(self.messages):
            if msg.get("role") == "assistant" and msg.get("content"):
                self._copy_to_clipboard(msg["content"])
                self.tool_status_label.configure(text="Copied last response")
                return
        messagebox.showinfo("Copy", "No assistant response to copy yet.")

    def _copy_chat_selection(self):
        try:
            selected = self._raw_text_widget.get("sel.first", "sel.last")
        except tk.TclError:
            messagebox.showinfo("Copy", "No text selected.")
            return
        self._copy_to_clipboard(selected)
        self.tool_status_label.configure(text="Copied selection")

    def _copy_all_chat_text(self):
        text = self.chat_box.get("1.0", "end-1c")
        self._copy_to_clipboard(text)
        self.tool_status_label.configure(text="Copied full chat log")

    def _show_chat_context_menu(self, event):
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Copy Selection", command=self._copy_chat_selection)
        menu.add_command(label="Copy Last Response", command=self.copy_last_response)
        menu.add_command(label="Copy All", command=self._copy_all_chat_text)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # -------------------------------------------------------- send / recv

    def _on_enter(self, event):
        self.send_message()
        return "break"

    def _on_shift_enter(self, event):
        self.input_box.insert("insert", "\n")
        return "break"

    def send_message(self):
        if self.is_generating:
            return
        typed_text = self.input_box.get("1.0", "end").strip()
        if not typed_text and not self.pending_image_path and not self.pending_file_path:
            return
        if not self.current_model or self.current_model.startswith("("):
            messagebox.showwarning("No model selected", "Select a model from the dropdown before chatting.")
            return
        if self.pending_image_path and not self.client.model_supports_vision(self.current_model):
            messagebox.showwarning(
                "Model doesn't support images",
                f"'{self.current_model}' doesn't support image input (Ollama would reject the request). "
                "Switch to a vision-capable model (e.g. llava, qwen2.5vl, llama3.2-vision) or remove the "
                "attached image before sending.",
            )
            return

        self.input_box.delete("1.0", "end")

        model_text_parts = []
        display_lines = []

        if self.pending_file_path:
            extracted = tools.file_ingest.extract_file_for_prompt(self.pending_file_path)
            fname = os.path.basename(self.pending_file_path)
            model_text_parts.append(f"--- Attached file: {fname} ---\n{extracted}\n--- end of attached file ---")
            display_lines.append(f"📄 Attached: {fname} ({len(extracted)} chars extracted)")

        if typed_text:
            model_text_parts.append(typed_text)
            display_lines.append(typed_text)
        elif self.pending_file_path:
            model_text_parts.append("Please review the attached file.")

        final_text = "\n\n".join(model_text_parts) if model_text_parts else "(image attached)"
        display_text = "\n".join(display_lines) if display_lines else "(image attached)"

        images_b64 = []
        thumb_path = None
        if self.pending_image_path:
            try:
                with open(self.pending_image_path, "rb") as f:
                    images_b64.append(base64.b64encode(f.read()).decode())
                thumb_path = self.pending_image_path
            except OSError as exc:
                messagebox.showerror("Attachment Error", f"Failed to read image: {exc}")
                return

        self._append_chat_block("user", display_text)
        if thumb_path:
            self._insert_image_thumbnail(thumb_path)
            self._insert("\n\n")

        user_msg = {"role": "user", "content": final_text}
        if images_b64:
            user_msg["images"] = images_b64
        self.messages.append(user_msg)
        add_message(self.current_session_id, "user", display_text)
        touch_session(self.current_session_id, self.current_model)
        self._refresh_sessions_list()

        self.pending_image_path = None
        self.pending_file_path = None
        self._refresh_attachment_bar()

        self.stop_flag.clear()
        self._assistant_header_inserted = False
        self._set_status_generating(True)

        working_messages = self._build_request_messages()
        threading.Thread(target=self._stream_worker, args=(working_messages,), daemon=True).start()

    def _build_request_messages(self) -> list[dict]:
        system_content = self.cfg.system_prompt + tools.workspace.workspace_prompt_context()
        msgs = [{"role": "system", "content": system_content}]
        msgs.extend(dict(m) for m in self.messages)
        return msgs

    def stop_generation(self):
        self.stop_flag.set()

    def _set_status_generating(self, active: bool):
        self.is_generating = active
        if active:
            self.send_btn.configure(state="disabled")
            self.stop_btn.configure(state="normal")
            self.tool_status_label.configure(text="Generating...")
        else:
            self.send_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            self.tool_status_label.configure(text="Idle")

    # -------------------------------------------------- background worker

    def _stream_worker(self, working_messages: list[dict]):
        def on_token(text):
            self.stream_queue.put({"type": "token", "content": text})

        def on_tool_round(content, calls):
            self.stream_queue.put({"type": "assistant_tool_round", "content": content, "calls": calls})

        def confirm_tool(name, args, category):
            req = ToolConfirmRequest(name, args, category)
            self.stream_queue.put({"type": "tool_confirm_request", "req": req})
            req.event.wait()
            if req.approved:
                self.stream_queue.put({"type": "tool_status", "text": f"Executing {name}..."})
            return req.approved

        def on_tool_result(name, args, result, image_path, image_b64):
            self.stream_queue.put(
                {
                    "type": "tool_result",
                    "name": name,
                    "args": args,
                    "result": result,
                    "image_path": image_path,
                    "image_b64": image_b64,
                }
            )

        def on_done(content, stopped):
            self.stream_queue.put({"type": "assistant_complete", "content": content, "stopped": stopped})

        def on_error(message):
            self.stream_queue.put({"type": "error", "message": message})

        hooks = chat_engine.EngineHooks(
            on_token=on_token,
            on_tool_round=on_tool_round,
            confirm_tool=confirm_tool,
            on_tool_result=on_tool_result,
            on_done=on_done,
            on_error=on_error,
            should_stop=lambda: self.stop_flag.is_set(),
        )
        chat_engine.run_chat_round_trip(
            self.client, self.current_model, working_messages, self.cfg.temperature, self.tool_execution_enabled, hooks
        )

    # ------------------------------------------------------------ polling

    def _poll_stream_queue(self):
        try:
            while True:
                item = self.stream_queue.get_nowait()
                self._handle_stream_item(item)
        except queue.Empty:
            pass
        self.after(30, self._poll_stream_queue)

    def _handle_stream_item(self, item: dict):
        t = item["type"]

        if t == "token":
            self._append_assistant_token(item["content"])

        elif t == "assistant_tool_round":
            self._end_assistant_block()
            content = item["content"]
            self.messages.append({"role": "assistant", "content": content, "tool_calls": item["calls"]})
            add_message(self.current_session_id, "assistant", content or "[tool call]")
            for call in item["calls"]:
                fn = call.get("function", {}) or {}
                self._append_tool_request_block(fn.get("name", ""), fn.get("arguments"))

        elif t == "tool_confirm_request":
            self._show_tool_confirm_dialog(item["req"])

        elif t == "tool_status":
            self.tool_status_label.configure(text=item["text"])

        elif t == "tool_result":
            self.messages.append({"role": "tool", "content": item["result"]})
            add_message(self.current_session_id, "tool", f"[{item['name']}] {item['result']}")
            self._append_tool_result_block(item["name"], item["result"])
            if item.get("image_b64"):
                self._insert_image_thumbnail(item["image_path"])
                self._insert("\n\n")
                self.messages.append(
                    {
                        "role": "user",
                        "content": "[Screenshot attached above for you to analyze]",
                        "images": [item["image_b64"]],
                    }
                )
            self.tool_status_label.configure(text="Generating...")

        elif t == "task_log_line":
            self._append_task_log_line(item["line"])

        elif t == "error":
            self._end_assistant_block()
            self._append_chat_block("system", f"Error: {item['message']}")
            self._set_status_generating(False)

        elif t == "assistant_complete":
            self._end_assistant_block()
            content = item["content"]
            if content:
                self.messages.append({"role": "assistant", "content": content})
                add_message(self.current_session_id, "assistant", content)
                touch_session(self.current_session_id, self.current_model)
                self._refresh_sessions_list()
            if item.get("stopped"):
                self._append_chat_block("system", "[stopped by user]")
            self._set_status_generating(False)

        elif t == "models_loaded":
            self._on_models_loaded(item["models"])

        elif t == "models_error":
            self.model_menu.configure(values=["(connection failed)"])
            self.model_menu.set("(connection failed)")

        elif t == "connection_status":
            if item["ok"]:
                self.conn_dot.configure(text_color=COLOR_OK)
                self.conn_label.configure(text="Connected")
            else:
                self.conn_dot.configure(text_color=COLOR_BAD)
                self.conn_label.configure(text="Disconnected")

        elif t == "telegram_test_result":
            if item["ok"]:
                self.telegram_status_label.configure(
                    text=f"Telegram: token valid (@{item['username']})", text_color=COLOR_OK
                )
            else:
                self.telegram_status_label.configure(text="Telegram: invalid token", text_color=COLOR_BAD)

        elif t == "hexstrike_test_result":
            if item["ok"]:
                self.hexstrike_status_label.configure(
                    text=f"HexStrike: connected (v{item['version']}, {item['tool_count']} tools)",
                    text_color=COLOR_OK,
                )
            else:
                self.hexstrike_status_label.configure(
                    text=f"HexStrike: unreachable — {item['error']}", text_color=COLOR_BAD
                )

        elif t == "docker_status":
            if item["available"]:
                self.sandbox_status_label.configure(text="Docker: available", text_color=COLOR_OK)
            else:
                self.sandbox_status_label.configure(
                    text="Docker: not found — install Docker Desktop, or leave sandboxing off",
                    text_color=COLOR_BAD,
                )

        elif t == "installed_models_result":
            self._render_installed_models(item["models"])

        elif t == "model_deleted":
            if item["result"]["ok"]:
                tools.task_log.log_event(f"[models] deleted '{item['name']}'")
                self.refresh_installed_models_tab()
                self._refresh_models_async()
            else:
                messagebox.showerror("Delete Failed", item["result"]["error"])

        elif t == "library_search_result":
            self._render_library_search_results(item["query"], item["results"])

        elif t == "pull_progress":
            self.model_pull_status.configure(text=item["status"])
            if item["fraction"] is not None:
                self.model_pull_progress.set(item["fraction"])

        elif t == "pull_done":
            self.pull_in_progress = False
            self.model_pull_progress.pack_forget()
            if item["result"]["ok"]:
                self.model_pull_status.configure(text=f"'{item['name']}' installed.")
                tools.task_log.log_event(f"[models] pulled '{item['name']}'")
                self.refresh_installed_models_tab()
                self._refresh_models_async()
            else:
                self.model_pull_status.configure(text=f"Pull failed: {item['result']['error']}")
                tools.task_log.log_event(f"[models] pull failed for '{item['name']}': {item['result']['error']}")

    def _render_installed_models(self, models):
        for w in self.installed_models_frame.winfo_children():
            w.destroy()
        if models is None:
            ctk.CTkLabel(
                self.installed_models_frame, text="Failed to load models — check connection.", text_color=COLOR_BAD
            ).pack(pady=8)
            return
        if not models:
            ctk.CTkLabel(self.installed_models_frame, text="No models installed.", text_color="gray60").pack(pady=8)
            return
        for name in models:
            row = ctk.CTkFrame(self.installed_models_frame, fg_color="transparent")
            row.pack(fill="x", pady=2)
            ctk.CTkLabel(row, text=name, anchor="w").pack(side="left", fill="x", expand=True)
            ctk.CTkButton(
                row,
                text="Delete",
                width=70,
                fg_color=COLOR_BAD,
                hover_color="#8A2E2C",
                command=lambda n=name: self.confirm_delete_model(n),
            ).pack(side="right")

    def _render_library_search_results(self, query, results):
        for w in self.model_search_frame.winfo_children():
            w.destroy()
        if not results:
            self.model_search_status.configure(text="No results (or ollama.com unreachable).")
            return
        label = f"{len(results)} result(s) for '{query}'" if query else f"{len(results)} popular models"
        self.model_search_status.configure(text=label)
        for r in results:
            self._add_search_result_row(r)

    def _create_dim_overlay(self):
        """Best-effort screen dim behind the confirmation dialog, like UAC's
        secure desktop switch. Purely cosmetic — failures here must never
        block the actual confirmation flow."""
        try:
            self.update_idletasks()
            overlay = ctk.CTkToplevel(self)
            overlay.overrideredirect(True)
            overlay.geometry(
                f"{self.winfo_width()}x{self.winfo_height()}+{self.winfo_x()}+{self.winfo_y()}"
            )
            overlay.configure(fg_color="black")
            overlay.attributes("-alpha", 0.55)
            overlay.attributes("-topmost", True)
            return overlay
        except Exception:
            return None

    def _draw_shield_icon(self, canvas: tk.Canvas):
        canvas.create_polygon(
            8, 10, 56, 10, 56, 30, 32, 58, 8, 30,
            fill=COLOR_WARN, outline="#B37A1E", width=2, smooth=True,
        )
        canvas.create_rectangle(29, 19, 35, 39, fill="#1E1E1E", outline="")
        canvas.create_oval(29, 43, 35, 49, fill="#1E1E1E", outline="")

    def _compute_confirm_diff(self, tool_name: str, args: dict):
        """Return a list of unified-diff lines for write_file/patch_file, or
        None for any other tool (falls back to showing raw args). Never raises
        — a diff that can't be computed just yields None so the dialog still
        works exactly as before."""
        try:
            if tool_name == "write_file":
                filepath = args.get("filepath")
                new_content = args.get("content", "")
                if not filepath:
                    return None
                resolved = tools.workspace.resolve_path(filepath)
                if os.path.isfile(resolved):
                    with open(resolved, "r", encoding="utf-8", errors="replace") as f:
                        old_content = f.read()
                    label = f"(overwriting {filepath})"
                else:
                    old_content = ""
                    label = f"(new file {filepath})"
                return self._unified_diff(old_content, new_content, label)

            if tool_name == "patch_file":
                filepath = args.get("filepath")
                search_str = args.get("search_str", "")
                replace_str = args.get("replace_str", "")
                if not filepath:
                    return None
                resolved = tools.workspace.resolve_path(filepath)
                if not os.path.isfile(resolved):
                    return None
                with open(resolved, "r", encoding="utf-8", errors="replace") as f:
                    old_content = f.read()
                count = old_content.count(search_str)
                if count != 1:
                    # Ambiguous or missing — patch_file will refuse anyway; show
                    # raw args so the reviewer sees exactly what was requested.
                    return None
                new_content = old_content.replace(search_str, replace_str, 1)
                return self._unified_diff(old_content, new_content, f"(patching {filepath})")
        except tools.workspace.WorkspaceError:
            return None
        except OSError:
            return None
        return None

    def _unified_diff(self, old_content: str, new_content: str, label: str):
        import difflib

        diff = list(
            difflib.unified_diff(
                old_content.splitlines(),
                new_content.splitlines(),
                lineterm="",
                n=3,
            )
        )
        if not diff:
            return [label, "(no textual changes)"]
        # Drop difflib's default '---'/'+++' header lines (blank filenames);
        # our own label is clearer.
        body = [ln for ln in diff if not (ln.startswith("---") or ln.startswith("+++"))]
        return [label, ""] + body[:400]

    def _show_tool_confirm_dialog(self, req: ToolConfirmRequest):
        overlay = self._create_dim_overlay()

        dlg = ctk.CTkToplevel(self)
        dlg.title("User Account Control")
        dlg.configure(fg_color="#1E1E1E")
        dlg.resizable(False, False)
        dlg.attributes("-topmost", True)

        width, height = 560, 480
        self.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() - width) // 2
        y = self.winfo_y() + (self.winfo_height() - height) // 2
        dlg.geometry(f"{width}x{height}+{x}+{y}")

        header = ctk.CTkFrame(dlg, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(20, 8))

        icon_canvas = tk.Canvas(header, width=64, height=64, bg="#1E1E1E", highlightthickness=0)
        icon_canvas.pack(side="left", padx=(0, 16))
        self._draw_shield_icon(icon_canvas)

        ctk.CTkLabel(
            header,
            text="Do you want to allow this app to run\nthe following tool on your computer?",
            font=("Segoe UI", 15, "bold"),
            justify="left",
            anchor="w",
        ).pack(side="left", fill="both", expand=True)

        ctk.CTkFrame(dlg, height=1, fg_color="#3A3A3A").pack(fill="x", padx=20, pady=(4, 12))

        info = ctk.CTkFrame(dlg, fg_color="transparent")
        info.pack(fill="x", padx=20)
        ctk.CTkLabel(info, text="Tool name:", text_color="gray60", anchor="w").pack(fill="x")
        ctk.CTkLabel(
            info, text=req.name, font=("Segoe UI", 16, "bold"), text_color=COLOR_WARN, anchor="w"
        ).pack(fill="x")
        risk_text = RISK_DESCRIPTIONS.get(req.category, RISK_DESCRIPTIONS["general"])
        ctk.CTkLabel(
            info,
            text=f"⚠ {risk_text}",
            text_color="gray60",
            anchor="w",
            justify="left",
            wraplength=500,
        ).pack(fill="x", pady=(4, 10))

        diff_lines = self._compute_confirm_diff(req.name, req.args)
        if diff_lines is not None:
            ctk.CTkLabel(dlg, text="Changes:", text_color="gray60", anchor="w").pack(fill="x", padx=20)
            details_box = ctk.CTkTextbox(dlg, height=190, font=("Consolas", 12))
            details_box.pack(fill="both", expand=True, padx=20, pady=(4, 12))
            raw = getattr(details_box, "_textbox", None)
            if raw is not None:
                raw.tag_config("diff_add", foreground="#7FD99A")
                raw.tag_config("diff_del", foreground="#E88", background="#3A2020")
                raw.tag_config("diff_meta", foreground="#7FB2E5")
                for line in diff_lines:
                    if line.startswith("+"):
                        tag = "diff_add"
                    elif line.startswith("-"):
                        tag = "diff_del"
                    elif line.startswith("@@") or line.startswith("(") or line.startswith("---") or line.startswith("+++"):
                        tag = "diff_meta"
                    else:
                        tag = None
                    raw.insert("end", line + "\n", (tag,) if tag else ())
            else:
                details_box.insert("1.0", "\n".join(diff_lines))
            details_box.configure(state="disabled")
        else:
            ctk.CTkLabel(dlg, text="Details:", text_color="gray60", anchor="w").pack(fill="x", padx=20)
            args_box = ctk.CTkTextbox(dlg, height=190, font=("Consolas", 12))
            args_box.pack(fill="both", expand=True, padx=20, pady=(4, 12))
            args_box.insert("1.0", json.dumps(req.args, indent=2))
            args_box.configure(state="disabled")

        def cleanup():
            if overlay is not None:
                overlay.destroy()

        def respond(approved: bool):
            req.approved = approved
            req.event.set()
            cleanup()
            dlg.destroy()

        btn_frame = ctk.CTkFrame(dlg, fg_color="transparent")
        btn_frame.pack(fill="x", padx=20, pady=(0, 20))
        ctk.CTkButton(
            btn_frame,
            text="No",
            width=120,
            fg_color="#2E5C8A",
            hover_color="#3A6FA5",
            command=lambda: respond(False),
        ).pack(side="right", padx=(8, 0))
        ctk.CTkButton(
            btn_frame,
            text="Yes",
            width=120,
            fg_color=COLOR_WARN,
            hover_color="#C48A22",
            text_color="#1E1E1E",
            command=lambda: respond(True),
        ).pack(side="right")

        dlg.protocol("WM_DELETE_WINDOW", lambda: respond(False))
        dlg.bind("<Escape>", lambda e: respond(False))

        if overlay is not None:
            dlg.lift(overlay)
        dlg.grab_set()
        dlg.focus_force()
        self.wait_window(dlg)

    # ------------------------------------------------------------- models

    def _refresh_models_async(self):
        threading.Thread(target=self._refresh_models_worker, daemon=True).start()

    def _refresh_models_worker(self):
        try:
            models = self.client.list_models()
        except Exception as exc:  # noqa: BLE001
            self.stream_queue.put({"type": "models_error", "message": str(exc)})
            return
        self.stream_queue.put({"type": "models_loaded", "models": models})

    def _on_models_loaded(self, models: list[str]):
        if not models:
            self.model_menu.configure(values=["(no models found)"])
            self.model_menu.set("(no models found)")
            return
        self.model_menu.configure(values=models)
        if self.current_model and self.current_model in models:
            self.model_menu.set(self.current_model)
        else:
            self.current_model = models[0]
            self.model_menu.set(self.current_model)
            self.cfg.last_model = self.current_model
            save_config(self.cfg)

    def on_model_change(self, value: str):
        # Switches the active model without touching self.messages, so the
        # running session's context carries over to the newly selected model.
        self.current_model = value
        self.cfg.last_model = value
        save_config(self.cfg)
        if self.current_session_id:
            touch_session(self.current_session_id, value)
            self._refresh_sessions_list()

    # --------------------------------------------------------- workspace

    def _update_workspace_label(self):
        folder = self.cfg.workspace_folder
        if folder:
            name = os.path.basename(folder.rstrip("/\\")) or folder
            lock = " 🔒" if self.cfg.workspace_sandboxed else ""
            self.workspace_label.configure(text=f"📁 {name}{lock}")
        else:
            self.workspace_label.configure(text="")

    def add_folder(self):
        folder = filedialog.askdirectory(title="Add workspace folder")
        if not folder:
            return
        self._set_workspace(folder)

    def _set_workspace(self, folder: str):
        self.cfg.workspace_folder = folder
        tools.state.workspace_folder = folder
        recent = [folder] + [p for p in self.cfg.recent_workspaces if p != folder]
        self.cfg.recent_workspaces = recent[:8]
        save_config(self.cfg)
        tools.state.workspace_sandboxed = self.cfg.workspace_sandboxed
        self._update_workspace_label()
        if hasattr(self, "recent_ws_menu"):
            self.recent_ws_menu.configure(values=self.cfg.recent_workspaces)
            self.recent_ws_menu.set(folder)
        self._append_chat_block("system", f"Workspace set to: {folder}")
        tools.task_log.log_event(f"[workspace] set to {folder}")

    def clear_workspace(self):
        self.cfg.workspace_folder = ""
        tools.state.workspace_folder = ""
        save_config(self.cfg)
        self._update_workspace_label()
        self._append_chat_block("system", "Workspace cleared.")

    def _estimate_context_usage(self):
        """Rough token estimate (~4 chars/token) of the current conversation
        plus system prompt and workspace context. Approximate — Ollama doesn't
        expose the live prompt token count until after a generation."""
        total_chars = len(self.cfg.system_prompt) + len(tools.workspace.workspace_prompt_context())
        for m in self.messages:
            total_chars += len(str(m.get("content", "")))
        return total_chars // 4

    def _update_context_label(self):
        approx_tokens = self._estimate_context_usage()
        if approx_tokens >= 1000:
            self.context_label.configure(text=f"~{approx_tokens // 1000}k ctx")
        else:
            self.context_label.configure(text=f"~{approx_tokens} ctx")

    # ------------------------------------------------------- tools manager

    def open_tools_manager(self):
        if self.tools_manager_dlg is not None:
            try:
                self.tools_manager_dlg.lift()
                self.tools_manager_dlg.focus_force()
                return
            except Exception:  # noqa: BLE001 - stale reference, rebuild
                self.tools_manager_dlg = None

        dlg = ctk.CTkToplevel(self)
        self.tools_manager_dlg = dlg
        dlg.title("Manage Tools")
        dlg.geometry("680x640")
        dlg.attributes("-topmost", True)

        def on_dlg_close():
            self.tools_manager_dlg = None
            dlg.destroy()

        dlg.protocol("WM_DELETE_WINDOW", on_dlg_close)

        header = ctk.CTkFrame(dlg, fg_color="transparent")
        header.pack(fill="x", padx=12, pady=(12, 4))
        ctk.CTkLabel(
            header,
            text="Toggle individual tools on/off. Disabled tools are hidden from the model entirely.\n"
            "The master 'Enable local tool execution' switch in Settings still governs everything.",
            justify="left",
            text_color="gray60",
            font=("", 11),
            anchor="w",
        ).pack(side="left", fill="x", expand=True)

        self.tools_search_entry = ctk.CTkEntry(dlg, placeholder_text="Filter tools by name or description...")
        self.tools_search_entry.pack(fill="x", padx=12, pady=(0, 4))
        self.tools_search_entry.bind("<KeyRelease>", lambda e: self._render_tools_manager())

        self.tools_manager_frame = ctk.CTkScrollableFrame(dlg, label_text="")
        self.tools_manager_frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        self._tool_switch_vars = {}
        self._render_tools_manager()

    def _render_tools_manager(self):
        for w in self.tools_manager_frame.winfo_children():
            w.destroy()
        filt = self.tools_search_entry.get().strip().lower()

        by_cat: dict[str, list] = {}
        for spec in tools.registry.specs():
            if filt and filt not in spec.name.lower() and filt not in spec.description.lower():
                continue
            by_cat.setdefault(spec.category, []).append(spec)

        if not by_cat:
            ctk.CTkLabel(self.tools_manager_frame, text="No tools match that filter.", text_color="gray60").pack(
                anchor="w", pady=8
            )
            return

        for category in sorted(by_cat):
            ctk.CTkLabel(
                self.tools_manager_frame, text=category.upper(), font=("", 12, "bold"), text_color=COLOR_WARN, anchor="w"
            ).pack(fill="x", pady=(10, 2))
            for spec in sorted(by_cat[category], key=lambda s: s.name):
                self._build_tool_row(spec)

    def _build_tool_row(self, spec):
        row = ctk.CTkFrame(self.tools_manager_frame, fg_color="#2A2A2A", corner_radius=6)
        row.pack(fill="x", pady=2)

        top = ctk.CTkFrame(row, fg_color="transparent")
        top.pack(fill="x", padx=8, pady=(6, 0))
        ctk.CTkLabel(top, text=spec.name, font=("Consolas", 13, "bold"), anchor="w").pack(side="left")

        var = ctk.BooleanVar(value=tools.registry.is_enabled(spec.name))
        self._tool_switch_vars[spec.name] = var
        ctk.CTkSwitch(
            top, text="", width=40, variable=var, command=lambda n=spec.name: self._toggle_tool(n)
        ).pack(side="right")

        params = spec.parameters.get("properties", {})
        param_str = ", ".join(params.keys()) if params else "no parameters"
        ctk.CTkLabel(
            row,
            text=f"{spec.description}\nParameters: {param_str}",
            justify="left",
            wraplength=580,
            text_color="gray70",
            font=("", 11),
            anchor="w",
        ).pack(fill="x", padx=8, pady=(0, 6))

    def _toggle_tool(self, name: str):
        var = self._tool_switch_vars.get(name)
        if var is None:
            return
        disabled = set(tools.registry.disabled_names())
        if var.get():
            disabled.discard(name)
        else:
            disabled.add(name)
        tools.registry.set_disabled(disabled)
        self.cfg.disabled_tools = sorted(disabled)
        save_config(self.cfg)

    # ------------------------------------------------------ model manager

    def open_model_manager(self):
        if self.model_manager_dlg is not None:
            try:
                self.model_manager_dlg.lift()
                self.model_manager_dlg.focus_force()
                return
            except Exception:  # noqa: BLE001 - stale reference, fall through and rebuild
                self.model_manager_dlg = None

        dlg = ctk.CTkToplevel(self)
        self.model_manager_dlg = dlg
        dlg.title("Manage Models")
        dlg.geometry("640x600")
        dlg.attributes("-topmost", True)

        def on_dlg_close():
            self.model_manager_dlg = None
            dlg.destroy()

        dlg.protocol("WM_DELETE_WINDOW", on_dlg_close)

        tabs = ctk.CTkTabview(dlg)
        tabs.pack(fill="both", expand=True, padx=12, pady=12)
        tab_installed = tabs.add("Installed")
        tab_discover = tabs.add("Discover")

        # --- Installed tab (remove)
        ctk.CTkButton(tab_installed, text="⟳ Refresh", width=90, command=self.refresh_installed_models_tab).pack(
            anchor="e", pady=(0, 6)
        )
        self.installed_models_frame = ctk.CTkScrollableFrame(tab_installed, label_text="")
        self.installed_models_frame.pack(fill="both", expand=True)
        self.refresh_installed_models_tab()

        # --- Discover tab (search + add)
        search_row = ctk.CTkFrame(tab_discover, fg_color="transparent")
        search_row.pack(fill="x", pady=(0, 4))
        self.model_search_entry = ctk.CTkEntry(
            search_row, placeholder_text="Search ollama.com models (e.g. 'llava', 'coder')..."
        )
        self.model_search_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.model_search_entry.bind("<Return>", lambda e: self.search_model_library())
        ctk.CTkButton(search_row, text="Search", width=90, command=self.search_model_library).pack(side="left")

        self.model_search_status = ctk.CTkLabel(tab_discover, text="", text_color="gray60", anchor="w")
        self.model_search_status.pack(fill="x")

        self.model_search_frame = ctk.CTkScrollableFrame(tab_discover, label_text="")
        self.model_search_frame.pack(fill="both", expand=True, pady=(4, 0))

        self.model_pull_status = ctk.CTkLabel(tab_discover, text="", text_color="gray60", anchor="w")
        self.model_pull_status.pack(fill="x", pady=(6, 0))
        self.model_pull_progress = ctk.CTkProgressBar(tab_discover)
        self.model_pull_progress.set(0)

        self.search_model_library()

    # --- installed / remove

    def refresh_installed_models_tab(self):
        for w in self.installed_models_frame.winfo_children():
            w.destroy()
        ctk.CTkLabel(self.installed_models_frame, text="Loading...", text_color="gray60").pack(pady=8)
        threading.Thread(target=self._installed_models_worker, daemon=True).start()

    def _installed_models_worker(self):
        try:
            models = self.client.list_models()
        except Exception:  # noqa: BLE001
            models = None
        self.stream_queue.put({"type": "installed_models_result", "models": models})

    def confirm_delete_model(self, name: str):
        if name == self.current_model:
            msg = f"'{name}' is your currently active model. Delete it anyway?"
        else:
            msg = f"Delete '{name}'? This frees disk space but you'll need to re-download it to use it again."
        if not messagebox.askyesno("Delete Model", msg):
            return
        threading.Thread(target=self._delete_model_worker, args=(name,), daemon=True).start()

    def _delete_model_worker(self, name: str):
        result = self.client.delete_model(name)
        self.stream_queue.put({"type": "model_deleted", "name": name, "result": result})

    # --- discover / search / pull

    def search_model_library(self):
        query = self.model_search_entry.get().strip()
        for w in self.model_search_frame.winfo_children():
            w.destroy()
        self.model_search_status.configure(text="Searching...")
        threading.Thread(target=self._search_library_worker, args=(query,), daemon=True).start()

    def _search_library_worker(self, query: str):
        results = self.client.search_library(query)
        self.stream_queue.put({"type": "library_search_result", "query": query, "results": results})

    def _add_search_result_row(self, r: dict):
        card = ctk.CTkFrame(self.model_search_frame, fg_color="#2A2A2A", corner_radius=6)
        card.pack(fill="x", pady=3, padx=2)

        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=10, pady=(8, 2))
        ctk.CTkLabel(top, text=r["name"], font=("", 14, "bold"), anchor="w").pack(side="left")
        ctk.CTkLabel(top, text=f"{r['pulls']} pulls", text_color="gray60", anchor="e").pack(side="right")

        if r["description"]:
            ctk.CTkLabel(
                card, text=r["description"], text_color="gray70", justify="left", wraplength=480, anchor="w"
            ).pack(fill="x", padx=10)

        bottom = ctk.CTkFrame(card, fg_color="transparent")
        bottom.pack(fill="x", padx=10, pady=(4, 8))
        if r["tags"]:
            ctk.CTkLabel(bottom, text=" · ".join(r["tags"]), text_color="#7FB2E5", anchor="w").pack(
                side="left", fill="x", expand=True
            )

        size_tags = [t for t in r["tags"] if re.match(r"^\d+(\.\d+)?[bm]$", t, re.IGNORECASE)]
        variant_values = ["latest"] + size_tags
        variant_menu = ctk.CTkOptionMenu(bottom, values=variant_values, width=90)
        variant_menu.set(variant_values[0])
        variant_menu.pack(side="right", padx=(6, 0))

        def do_pull():
            variant = variant_menu.get()
            full_name = r["name"] if variant == "latest" else f"{r['name']}:{variant}"
            self.start_model_pull(full_name)

        ctk.CTkButton(bottom, text="⬇ Pull", width=70, command=do_pull).pack(side="right")

    def start_model_pull(self, name: str):
        if self.pull_in_progress:
            messagebox.showinfo("Pull in progress", "Wait for the current model pull to finish first.")
            return
        self.pull_in_progress = True
        self.pull_stop_flag.clear()
        self.model_pull_progress.pack(fill="x", pady=(2, 0))
        self.model_pull_progress.set(0)
        self.model_pull_status.configure(text=f"Pulling '{name}'...")
        threading.Thread(target=self._pull_model_worker, args=(name,), daemon=True).start()

    def _pull_model_worker(self, name: str):
        def on_progress(chunk):
            total = chunk.get("total")
            completed = chunk.get("completed")
            fraction = (completed / total) if (total and completed and total > 0) else None
            self.stream_queue.put({"type": "pull_progress", "status": chunk.get("status", ""), "fraction": fraction})

        result = self.client.pull_model(name, on_progress=on_progress, should_stop=lambda: self.pull_stop_flag.is_set())
        self.stream_queue.put({"type": "pull_done", "name": name, "result": result})

    # -------------------------------------------------------- connection

    def _poll_connection_loop(self):
        threading.Thread(target=self._check_connection_worker, daemon=True).start()
        self.after(8000, self._poll_connection_loop)

    def _check_connection_worker(self):
        ok = self.client.check_connection()
        self.stream_queue.put({"type": "connection_status", "ok": ok})

    # ---------------------------------------------------------- sessions

    def _ensure_session(self):
        sessions = list_sessions()
        if sessions:
            self._load_session(sessions[0]["id"])
        else:
            self.new_session()

    def new_session(self):
        name = f"Session {time.strftime('%Y-%m-%d %H:%M:%S')}"
        sid = create_session(name, self.current_model or "")
        self.current_session_id = sid
        self.messages = []
        self._clear_chat_display()
        self._refresh_sessions_list()

    def _load_session(self, session_id: int):
        self.current_session_id = session_id
        rows = load_messages(session_id)
        self.messages = [{"role": r["role"], "content": r["content"]} for r in rows]
        self._clear_chat_display()
        for r in rows:
            if r["role"] == "tool":
                self._append_tool_result_block("tool", r["content"])
            else:
                self._append_chat_block(r["role"], r["content"])
        self._refresh_sessions_list()

    def delete_current_session(self):
        if not self.current_session_id:
            return
        if not messagebox.askyesno("Delete Session", "Delete this session and all its messages?"):
            return
        delete_session(self.current_session_id)
        sessions = list_sessions()
        if sessions:
            self._load_session(sessions[0]["id"])
        else:
            self.new_session()

    def clear_current_chat(self):
        if not self.current_session_id:
            return
        if not messagebox.askyesno("Clear Chat", "Clear all messages in this session?"):
            return
        clear_session_messages(self.current_session_id)
        self.messages = []
        self._clear_chat_display()

    def _refresh_sessions_list(self):
        for widget in self.sessions_list_frame.winfo_children():
            widget.destroy()
        for s in list_sessions():
            is_current = s["id"] == self.current_session_id
            ctk.CTkButton(
                self.sessions_list_frame,
                text=s["name"],
                fg_color="#2E5C8A" if is_current else "transparent",
                anchor="w",
                command=lambda sid=s["id"]: self._load_session(sid),
            ).pack(fill="x", pady=2)

    # ----------------------------------------------------------- settings

    def save_system_prompt(self):
        self.cfg.system_prompt = self.system_prompt_box.get("1.0", "end").strip()
        save_config(self.cfg)
        messagebox.showinfo("Saved", "System prompt saved. It will apply to your next message.")

    def apply_base_url(self):
        url = self.base_url_entry.get().strip()
        if not url:
            return
        self.cfg.base_url = url
        save_config(self.cfg)
        self.client.set_base_url(url)
        self._refresh_models_async()

    def on_temp_change(self, value):
        value = float(value)
        self.cfg.temperature = value
        self.temp_label.configure(text=f"{value:.2f}")
        save_config(self.cfg)

    def on_appearance_mode_change(self, value):
        mode = value.lower()
        self.cfg.appearance_mode = mode
        save_config(self.cfg)
        ctk.set_appearance_mode(mode)

    def on_color_theme_change(self, value):
        self.cfg.color_theme = value.lower()
        save_config(self.cfg)

    def on_chat_font_change(self, value):
        self.cfg.chat_font_family = value
        save_config(self.cfg)
        self._configure_chat_tags()

    def on_chat_font_size_change(self, value):
        size = int(value)
        self.cfg.chat_font_size = size
        self.chat_font_size_label.configure(text=str(size))
        save_config(self.cfg)
        self._configure_chat_tags()

    def pick_color(self, field_name: str):
        current = getattr(self.cfg, field_name)
        _, hex_color = colorchooser.askcolor(color=current, title="Choose color")
        if not hex_color:
            return
        setattr(self.cfg, field_name, hex_color)
        save_config(self.cfg)
        messagebox.showinfo("Saved", "Color saved. Restart the app to apply it everywhere.")

    def save_brave_key(self):
        self.cfg.brave_api_key = self.brave_key_entry.get().strip()
        save_config(self.cfg)
        messagebox.showinfo("Saved", "Brave Search API key saved.")

    def save_nvd_key(self):
        self.cfg.nvd_api_key = self.nvd_key_entry.get().strip()
        save_config(self.cfg)
        messagebox.showinfo("Saved", "NVD API key saved.")

    def save_and_test_hexstrike(self):
        url = self.hexstrike_url_entry.get().strip()
        self.cfg.hexstrike_base_url = url
        save_config(self.cfg)

        if not url:
            self.hexstrike_status_label.configure(text="HexStrike: no server URL set", text_color="gray60")
            return
        self.hexstrike_status_label.configure(text="HexStrike: testing...", text_color="gray60")
        threading.Thread(target=self._test_hexstrike_worker, args=(url,), daemon=True).start()

    def _test_hexstrike_worker(self, url: str):
        try:
            resp = requests.get(f"{url.rstrip('/')}/health", timeout=10)
            resp.raise_for_status()
            data = resp.json()
            self.stream_queue.put(
                {
                    "type": "hexstrike_test_result",
                    "ok": True,
                    "version": data.get("version", "?"),
                    "tool_count": data.get("total_tools_count", "?"),
                }
            )
        except Exception as exc:  # noqa: BLE001
            self.stream_queue.put({"type": "hexstrike_test_result", "ok": False, "error": str(exc)})

    def on_hexstrike_toggle(self):
        self.cfg.hexstrike_enabled = self.hexstrike_switch_var.get()
        save_config(self.cfg)

    def on_plugins_toggle(self):
        self.cfg.plugins_enabled = self.plugins_switch_var.get()
        save_config(self.cfg)

    def open_plugins_folder(self):
        try:
            os.startfile(tools.plugin_loader.PLUGINS_DIR)
        except OSError as exc:
            messagebox.showerror("Error", f"Could not open plugins folder: {exc}")

    def reload_plugins_ui(self):
        tools.reload_plugins()
        self._refresh_plugins_status()

    def _refresh_plugins_status(self):
        results = tools.loaded_plugins
        if not results:
            self.plugins_status_label.configure(text="No plugins found.")
            return
        lines = []
        for r in results:
            if r["ok"]:
                names = ", ".join(r["tool_names_added"]) or "(no tools registered)"
                lines.append(f"✅ {r['file']}: {names}")
            else:
                lines.append(f"❌ {r['file']}: {r['error']}")
        self.plugins_status_label.configure(text="\n".join(lines))

    def on_tool_toggle(self):
        self.tool_execution_enabled = self.tool_switch_var.get()
        self.cfg.tool_execution_enabled = self.tool_execution_enabled
        tools.state.tool_execution_enabled = self.tool_execution_enabled
        save_config(self.cfg)

    def on_sandbox_toggle(self):
        self.cfg.sandbox_enabled = self.sandbox_switch_var.get()
        tools.state.sandbox_enabled = self.cfg.sandbox_enabled
        save_config(self.cfg)
        self._refresh_sandbox_status()

    def on_recent_workspace_selected(self, value: str):
        if value and value != "(none yet)" and os.path.isdir(value):
            self._set_workspace(value)

    def on_workspace_sandbox_toggle(self):
        self.cfg.workspace_sandboxed = self.workspace_sandbox_var.get()
        tools.state.workspace_sandboxed = self.cfg.workspace_sandboxed
        save_config(self.cfg)
        self._update_workspace_label()

    def _refresh_sandbox_status(self):
        self.sandbox_status_label.configure(text="Checking Docker...", text_color="gray60")
        threading.Thread(target=self._check_docker_worker, daemon=True).start()

    def _check_docker_worker(self):
        available = tools.sandbox.docker_available()
        self.stream_queue.put({"type": "docker_status", "available": available})

    # ---------------------------------------------------------- telegram

    def save_and_test_telegram(self):
        token = self.telegram_token_entry.get().strip()
        ids_raw = self.telegram_ids_entry.get().strip()
        self.cfg.telegram_bot_token = token
        self.cfg.telegram_allowed_user_ids = ids_raw
        save_config(self.cfg)

        if not token:
            self.telegram_status_label.configure(text="Telegram: no token set", text_color="gray60")
            return
        self.telegram_status_label.configure(text="Telegram: testing...", text_color="gray60")
        threading.Thread(target=self._test_telegram_worker, args=(token,), daemon=True).start()

    def _test_telegram_worker(self, token: str):
        info = telegram_bridge.TelegramBridge(token, set()).get_me()
        self.stream_queue.put(
            {"type": "telegram_test_result", "ok": bool(info), "username": (info or {}).get("username")}
        )

    def on_telegram_toggle(self):
        enabled = self.telegram_switch_var.get()
        self.cfg.telegram_enabled = enabled
        save_config(self.cfg)
        if enabled:
            self._start_telegram_bridge()
        else:
            self._stop_telegram_bridge()

    def _get_or_create_telegram_session(self) -> int:
        for s in list_sessions():
            if s["name"] == "📱 Telegram":
                return s["id"]
        return create_session("📱 Telegram", self.current_model or "")

    def _start_telegram_bridge(self):
        token = self.cfg.telegram_bot_token.strip()
        ids_raw = self.cfg.telegram_allowed_user_ids.strip()
        if not token or not ids_raw:
            messagebox.showwarning("Telegram", "Set a bot token and at least one allowed user ID first.")
            self._telegram_enable_failed()
            return
        try:
            allowed_ids = {int(x.strip()) for x in ids_raw.split(",") if x.strip()}
        except ValueError:
            messagebox.showerror("Telegram", "Allowed User IDs must be comma-separated numbers.")
            self._telegram_enable_failed()
            return
        if not allowed_ids:
            messagebox.showwarning("Telegram", "Set at least one allowed user ID first.")
            self._telegram_enable_failed()
            return

        self.telegram_session_id = self._get_or_create_telegram_session()
        rows = load_messages(self.telegram_session_id)
        self.telegram_messages = [{"role": r["role"], "content": r["content"]} for r in rows]

        if self.telegram_bridge_instance is not None:
            self.telegram_bridge_instance.stop()

        self.telegram_bridge_instance = telegram_bridge.TelegramBridge(
            token, allowed_ids, on_message=self._on_telegram_message, on_callback=self._on_telegram_callback
        )
        self.telegram_bridge_instance.start()
        self.telegram_status_label.configure(text="Telegram: running", text_color=COLOR_OK)
        tools.task_log.log_event("[telegram] bridge started")

    def _telegram_enable_failed(self):
        self.telegram_switch_var.set(False)
        self.cfg.telegram_enabled = False
        save_config(self.cfg)

    def _stop_telegram_bridge(self):
        if self.telegram_bridge_instance is not None:
            self.telegram_bridge_instance.stop()
            self.telegram_bridge_instance = None
        self.telegram_status_label.configure(text="Telegram: stopped", text_color="gray60")
        tools.task_log.log_event("[telegram] bridge stopped")

    def _on_telegram_message(self, chat_id: int, text: str):
        # Called from the bridge's background polling thread.
        text = text.strip()
        if text == "/start":
            self.telegram_bridge_instance.send_message(
                chat_id, "Local AI Assistant is connected. Send a message to chat, /new to reset, /status for status."
            )
            return
        if text == "/new":
            self.telegram_messages = []
            if self.telegram_session_id:
                clear_session_messages(self.telegram_session_id)
            self.telegram_bridge_instance.send_message(chat_id, "Conversation reset.")
            return
        if text == "/status":
            status = (
                f"Model: {self.current_model or '(none)'}\n"
                f"Tool execution: {'enabled' if self.tool_execution_enabled else 'disabled'}"
            )
            self.telegram_bridge_instance.send_message(chat_id, status)
            return

        if not self.telegram_lock.acquire(blocking=False):
            self.telegram_bridge_instance.send_message(chat_id, "Still working on your previous message — please wait.")
            return
        threading.Thread(target=self._telegram_worker, args=(chat_id, text), daemon=True).start()

    def _telegram_worker(self, chat_id: int, text: str):
        tools.task_log.log_event(f"[telegram] message from chat {chat_id}: {text[:120]!r}")
        try:
            self.telegram_messages.append({"role": "user", "content": text})
            add_message(self.telegram_session_id, "user", text)
            touch_session(self.telegram_session_id, self.current_model)

            working_messages = [{"role": "system", "content": self.cfg.system_prompt}]
            working_messages.extend(dict(m) for m in self.telegram_messages)

            def on_token(_t):
                pass  # Telegram gets one complete reply per turn, not live tokens.

            def on_tool_round(content, calls):
                if content:
                    self.telegram_bridge_instance.send_message(chat_id, content)
                for call in calls:
                    fn = call.get("function", {}) or {}
                    args_pretty = json.dumps(fn.get("arguments", {}), indent=2)
                    self.telegram_bridge_instance.send_message(chat_id, f"🔧 Calling {fn.get('name', '')}\n{args_pretty}")
                self.telegram_messages.append({"role": "assistant", "content": content, "tool_calls": calls})
                add_message(self.telegram_session_id, "assistant", content or "[tool call]")

            def confirm_tool(name, args, category):
                return self._telegram_confirm_tool(chat_id, name, args, category)

            def on_tool_result(name, args, result, image_path, image_b64):
                self.telegram_messages.append({"role": "tool", "content": result})
                add_message(self.telegram_session_id, "tool", f"[{name}] {result}")
                self.telegram_bridge_instance.send_message(chat_id, f"✅ {name} result:\n{result}")
                if image_b64 and image_path:
                    self.telegram_bridge_instance.send_photo(chat_id, image_path)
                    self.telegram_messages.append(
                        {
                            "role": "user",
                            "content": "[Screenshot attached above for you to analyze]",
                            "images": [image_b64],
                        }
                    )

            def on_done(content, stopped):
                if content:
                    self.telegram_bridge_instance.send_message(chat_id, content)
                    self.telegram_messages.append({"role": "assistant", "content": content})
                    add_message(self.telegram_session_id, "assistant", content)
                    touch_session(self.telegram_session_id, self.current_model)
                tools.task_log.log_event(f"[telegram] replied to chat {chat_id}")

            def on_error(message):
                self.telegram_bridge_instance.send_message(chat_id, f"⚠ Error: {message}")
                tools.task_log.log_event(f"[telegram] error for chat {chat_id}: {message}")

            hooks = chat_engine.EngineHooks(
                on_token=on_token,
                on_tool_round=on_tool_round,
                confirm_tool=confirm_tool,
                on_tool_result=on_tool_result,
                on_done=on_done,
                on_error=on_error,
            )
            chat_engine.run_chat_round_trip(
                self.client, self.current_model, working_messages, self.cfg.temperature,
                self.tool_execution_enabled, hooks,
            )
        finally:
            self.telegram_lock.release()

    def _telegram_confirm_tool(self, chat_id: int, name: str, args: dict, category: str) -> bool:
        req_id = uuid.uuid4().hex[:8]
        event = threading.Event()
        decision = {"approved": False}
        self.telegram_pending_confirms[req_id] = (event, decision)

        risk = RISK_DESCRIPTIONS.get(category, RISK_DESCRIPTIONS["general"])
        args_preview = json.dumps(args, indent=2)[:3000]
        text = f"🛡 Tool request: {name}\n{risk}\n\nArgs:\n{args_preview}"
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ Yes", "callback_data": f"tc:{req_id}:y"},
                    {"text": "❌ No", "callback_data": f"tc:{req_id}:n"},
                ]
            ]
        }
        self.telegram_bridge_instance.send_message(chat_id, text, reply_markup=keyboard)

        responded = event.wait(timeout=600)
        self.telegram_pending_confirms.pop(req_id, None)
        if not responded:
            self.telegram_bridge_instance.send_message(chat_id, f"⏱ Confirmation for '{name}' timed out — denied.")
            return False
        return decision["approved"]

    def _on_telegram_callback(self, cq: dict):
        # Called from the bridge's background polling thread.
        data = cq.get("data", "")
        cq_id = cq.get("id", "")
        message = cq.get("message", {}) or {}
        chat_id = message.get("chat", {}).get("id")
        message_id = message.get("message_id")

        parts = data.split(":")
        if len(parts) != 3 or parts[0] != "tc":
            self.telegram_bridge_instance.answer_callback(cq_id)
            return
        _, req_id, decision_code = parts
        pending = self.telegram_pending_confirms.get(req_id)
        self.telegram_bridge_instance.answer_callback(cq_id, "Recorded" if pending else "Expired")
        if not pending:
            return
        event, decision = pending
        decision["approved"] = decision_code == "y"
        event.set()
        label = "✅ Approved" if decision["approved"] else "❌ Denied"
        if chat_id is not None and message_id is not None:
            self.telegram_bridge_instance.edit_message_text(chat_id, message_id, label)

    # -------------------------------------------------------------- close

    def on_close(self):
        self.cfg.window_geometry = self.geometry()
        save_config(self.cfg)
        tools.shutdown_scheduler()
        self._stop_telegram_bridge()
        self.destroy()


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
