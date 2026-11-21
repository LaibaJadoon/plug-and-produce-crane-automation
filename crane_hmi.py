# Its functionality includes:
# - Manual crane controls.
# - Saved crane positions.
# - Editing JSON sequences.
# - Loading and saving JSON files.
# - Executing sequences through the interface.

import json
import os
import threading   # Allows tasks to run in separate threads without freezing the GUI.
import time
import tkinter as tk  # Provides the GUI framework for the HMI.
from tkinter import ttk, messagebox, filedialog # Provides themed widgets, message boxes, and file dialogs for the GUI.
from pymodbus.client import ModbusTcpClient  # Provides the Modbus TCP client for communication with the crane simulator.

HOST, PORT = "127.0.0.1", 502
JSON_FILE = "crane_sequence.json"
SET_X, SET_Y, VACUUM, START_P1, START_P2 = 1, 2, 3, 4, 5
AT_X, AT_Y = 15, 16
SOURCE1, SOURCE2, P1_RUNNING, P2_RUNNING, P1_SENSOR, P2_SENSOR = 17, 18, 19, 20, 21, 22
SAFE_Y, MOVE_TIMEOUT = 200, 20

DEFAULT_POSITIONS = {
    "Source 1 (pick)": {"setX": 55, "setY": 82},
    "Source 1 (safe)": {"setX": 55, "setY": 200},
    "Source 2 (pick)": {"setX": 158, "setY": 82},
    "Source 2 (safe)": {"setX": 158, "setY": 200},
    "Process 1 (pick/place)": {"setX": 450, "setY": 82},
    "Process 1 (safe)": {"setX": 450, "setY": 200},
    "Process 2 (pick/place)": {"setX": 650, "setY": 82},
    "Process 2 (safe)": {"setX": 650, "setY": 200},
    "Sink (place)": {"setX": 945, "setY": 82},
    "Sink (safe)": {"setX": 945, "setY": 200},
}

# The CraneHMI class encapsulates the entire functionality of the crane automation HMI.
# It manages the GUI, Modbus communication, and user interactions.

class CraneHMI:
    def __init__(self, root):
        self.root = root
        root.title("Crane Automation — Grade A HMI TASK")
        root.geometry("1100x760")
        root.minsize(850, 550)
        self.client = ModbusTcpClient(HOST, port=PORT) # Creates a Modbus TCP client for communication with the crane simulator.
        self.connected = False
        self.busy = False # Indicates whether a crane operation is currently in progress, preventing overlapping commands.
        self.lock = threading.Lock() # A threading lock to ensure that only one crane operation can be executed at a time, preventing race conditions.
        self.positions = {k: dict(v) for k, v in DEFAULT_POSITIONS.items()} # Initializes the saved crane positions with default values, allowing users to quickly access common positions without manual entry.
        self.actions = [] # Initializes the list of actions that will be executed in sequence, allowing users to build and modify a series of crane operations.

        self.status = tk.StringVar(value="Not connected. Start the simulator, then click Connect.")
        self.x_text, self.y_text = tk.StringVar(value="X: —"), tk.StringVar(value="Y: —")
        self.vac_text = tk.StringVar(value="Vacuum: —")
        self.p1_text, self.p2_text = tk.StringVar(value="Process 1: —"), tk.StringVar(value="Process 2: —")
        self.target_x, self.target_y = tk.StringVar(value="55"), tk.StringVar(value="200")
        self.selected_position = tk.StringVar()
        self.position_name = tk.StringVar()
        self.description = tk.StringVar(value="Grade A sequence created with the HMI")

# prepare the HMI after initializing its variables
        self.build_ui()
        self.refresh_positions()
        self.load_path(JSON_FILE, False)
        self.poll_status()
        root.protocol("WM_DELETE_WINDOW", self.close)

    # Building the graphical interface
    def build_ui(self):
        # Scrollable main area so all controls remain reachable on smaller screens.
        outer = ttk.Frame(self.root)
        outer.pack(fill="both", expand=True)


        # The canvas widget is used to create a scrollable area within the main window. It allows for the placement of other widgets inside it, enabling scrolling when the content exceeds the visible area.
        self.canvas = tk.Canvas(outer, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.content = ttk.Frame(self.canvas)
        self.content_window = self.canvas.create_window((0, 0), window=self.content, anchor="nw")

        def update_scroll_region(_event=None):
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))

        def resize_content(event):
            self.canvas.itemconfigure(self.content_window, width=event.width)

        self.content.bind("<Configure>", update_scroll_region)
        self.canvas.bind("<Configure>", resize_content)

        # Mouse-wheel scrolling on Windows.
        self.canvas.bind_all(
            "<MouseWheel>",
            lambda event: self.canvas.yview_scroll(int(-event.delta / 120), "units")
        )

        # Creating the HMI header
        header = ttk.Frame(self.content, padding=10)
        header.pack(fill="x")
        ttk.Label(header, text="Crane Automation — Grade A HMI", font=("Segoe UI", 16, "bold")).pack(side="left")
        ttk.Button(header, text="Connect / Reconnect", command=self.connect).pack(side="right")


        # Creating the live status display
        live = ttk.LabelFrame(self.content, text="Live simulation status", padding=8)
        live.pack(fill="x", padx=10, pady=4)
        ttk.Label(live, textvariable=self.status).grid(row=0, column=0, columnspan=5, sticky="w", pady=(0, 6))
        for col, var in enumerate((self.x_text, self.y_text, self.vac_text, self.p1_text, self.p2_text)):
            ttk.Label(live, textvariable=var, font=("Segoe UI", 10, "bold")).grid(row=1, column=col, padx=8, sticky="w")
        # Creating the manual control section
        manual = ttk.LabelFrame(self.content, text="Manual crane control", padding=8)
        manual.pack(fill="x", padx=10, pady=4)
        ttk.Label(manual, text="X").grid(row=0, column=0)
        ttk.Entry(manual, textvariable=self.target_x, width=9).grid(row=0, column=1, padx=4)
        ttk.Label(manual, text="Y").grid(row=0, column=2)
        ttk.Entry(manual, textvariable=self.target_y, width=9).grid(row=0, column=3, padx=4)
        ttk.Button(manual, text="Move crane", command=self.manual_move).grid(row=0, column=4, padx=4)
        ttk.Button(manual, text="Vacuum ON", command=lambda: self.manual_write(VACUUM, 1)).grid(row=1, column=0, pady=6)
        ttk.Button(manual, text="Vacuum OFF", command=lambda: self.manual_write(VACUUM, 0)).grid(row=1, column=1, pady=6)
        ttk.Button(manual, text="Start Process 1", command=lambda: self.manual_write(START_P1, 1)).grid(row=1, column=2, pady=6)
        ttk.Button(manual, text="Start Process 2", command=lambda: self.manual_write(START_P2, 1)).grid(row=1, column=3, pady=6)
        ttk.Label(manual, text="Movement automatically raises to safe Y=200 before horizontal travel.").grid(row=2, column=0, columnspan=5, sticky="w")

        bottom = ttk.LabelFrame(self.content, text="Load, save and execute JSON", padding=8)
        bottom.pack(fill="x", padx=10, pady=5)
        ttk.Label(bottom, text="Description").grid(row=0, column=0, sticky="w")
        ttk.Entry(bottom, textvariable=self.description).grid(row=0, column=1, sticky="ew", padx=5)
        ttk.Button(bottom, text="Load JSON", command=lambda: self.load_path(JSON_FILE, True)).grid(row=0, column=2, padx=3)
        ttk.Button(bottom, text="Save JSON", command=lambda: self.save_path(JSON_FILE)).grid(row=0, column=3, padx=3)
        ttk.Button(bottom, text="Load file…", command=self.choose_load).grid(row=1, column=0, pady=7, sticky="ew")
        ttk.Button(bottom, text="Save As…", command=self.choose_save).grid(row=1, column=1, pady=7, sticky="ew")
        ttk.Button(bottom, text="EXECUTE SEQUENCE", command=self.execute).grid(row=1, column=2, columnspan=2, pady=7, sticky="ew")
        bottom.columnconfigure(1, weight=1)
        # Creating the main body of the HMI with saved positions and JSON sequence builder
        body = ttk.Frame(self.content, padding=(10, 4))
        body.pack(fill="both", expand=True)
        left = ttk.LabelFrame(body, text="Saved positions (all X/Y coordinates)", padding=8)
        left.pack(side="left", fill="both", expand=True, padx=(0, 5))
        self.tree = ttk.Treeview(left, columns=("x", "y"), show="headings", height=10)
        self.tree.heading("x", text="X")
        self.tree.heading("y", text="Y")
        self.tree.column("x", width=65, anchor="center")
        self.tree.column("y", width=65, anchor="center")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.position_selected)
        ttk.Label(left, text="Name (for adding a position)").pack(anchor="w", pady=(6, 0))
        ttk.Entry(left, textvariable=self.position_name).pack(fill="x")
        ttk.Button(left, text="Update selected position to crane's current X/Y", command=self.update_position).pack(fill="x", pady=3)
        ttk.Button(left, text="Add position using target X/Y", command=self.add_position).pack(fill="x", pady=3)
        ttk.Button(left, text="Move to selected position", command=self.move_selected).pack(fill="x", pady=3)

        # Creating the right side of the HMI for building the JSON sequence
        right = ttk.LabelFrame(body, text="Build the JSON sequence", padding=8)
        right.pack(side="left", fill="both", expand=True, padx=(5, 0))
        ttk.Label(right, text="Select a position to add a move:").pack(anchor="w")
        self.combo = ttk.Combobox(right, textvariable=self.selected_position, state="readonly")
        self.combo.pack(fill="x", pady=4)
        ttk.Button(right, text="Add selected move", command=self.add_move).pack(fill="x", pady=2)
        row1 = ttk.Frame(right); row1.pack(fill="x", pady=3)
        ttk.Button(row1, text="Add Vacuum ON", command=lambda: self.add_action({"vacuum": 1})).pack(side="left", expand=True, fill="x", padx=2)
        ttk.Button(row1, text="Add Vacuum OFF", command=lambda: self.add_action({"vacuum": 0})).pack(side="left", expand=True, fill="x", padx=2)
        row2 = ttk.Frame(right); row2.pack(fill="x", pady=3)
        ttk.Button(row2, text="Add Start P1", command=lambda: self.add_action({"startp1": 1})).pack(side="left", expand=True, fill="x", padx=2)
        ttk.Button(row2, text="Add Start P2", command=lambda: self.add_action({"startp2": 1})).pack(side="left", expand=True, fill="x", padx=2)
        ttk.Label(right, text="Sequence actions in execution order:").pack(anchor="w", pady=(5, 0))
        self.action_list = tk.Listbox(right, height=7)
        self.action_list.pack(fill="both", expand=True, pady=4)
        row3 = ttk.Frame(right); row3.pack(fill="x")
        ttk.Button(row3, text="Remove selected", command=self.remove_action).pack(side="left", expand=True, fill="x", padx=2)
        ttk.Button(row3, text="Clear sequence", command=self.clear_actions).pack(side="left", expand=True, fill="x", padx=2)


    # Modbus communication methods for reading and writing registers, as well as waiting for the crane to reach a target position.
    def read(self, addr):
        r = self.client.read_holding_registers(address=addr, count=1)
        if r.isError():
            raise RuntimeError(f"Read failed for Modbus register {addr}")
        return r.registers[0]
    
    # The write method sends a value to a specified Modbus register address. It checks for errors in the response and raises an exception if the write operation fails.
    def write(self, addr, value):
        r = self.client.write_register(address=addr, value=int(value))
        if r.isError():
            raise RuntimeError(f"Write failed for Modbus register {addr}")

        
    # The wait method continuously checks the value of a specified Modbus register until it matches a target value or a timeout occurs. It raises a TimeoutError if the crane does not reach the target within the defined MOVE_TIMEOUT period.
    def wait(self, addr, target):
        start = time.time()
        while self.read(addr) != target:
            if time.time() - start > MOVE_TIMEOUT:
                raise TimeoutError(f"Crane did not reach {target} within {MOVE_TIMEOUT} seconds.")
            time.sleep(0.05)
    # Moves the crane safely to the requested X and Y coordinates. 
    def move_crane(self, x, y):
        x, y = int(x), int(y)
        if not (0 <= x <= 1000 and 0 <= y <= 1000):
            raise ValueError("Enter X and Y values between 0 and 1000.")
        self.write(SET_Y, SAFE_Y); self.wait(AT_Y, SAFE_Y)
        self.write(SET_X, x); self.wait(AT_X, x)
        if y != SAFE_Y:
            self.write(SET_Y, y); self.wait(AT_Y, y)

    #  Connects the HMI to the crane simulator in the background. Raise an error if the connection fails, and update the status accordingly. This method ensures that the GUI remains responsive during the connection attempt.
    def connect(self):
        def task():
            if not self.client.connect():
                raise ConnectionError("Could not connect to 127.0.0.1:502. Start the simulator first.")
            self.connected = True
            self.set_status("Connected to simulator.")
            self.update_live()
        self.background(task, "Connecting…")

    # takes the manually entered X and Y coordinates from the HMI and moves the crane to that position.
    def manual_move(self):
        try: x, y = int(self.target_x.get()), int(self.target_y.get())
        except ValueError:
            messagebox.showerror("Invalid position", "X and Y must be whole numbers."); return
        self.background(lambda: self.move_crane(x, y), "Moving crane…")


    # sends a command to a specified address with a given value.
    def manual_write(self, addr, value):
        self.background(lambda: self.write(addr, value), "Sending command…")


    # Reads the crane and process information and updates the HMI display.
    def update_live(self):
        x, y, vac = self.read(AT_X), self.read(AT_Y), self.read(VACUUM)
        p1r, p1s, p2r, p2s = self.read(P1_RUNNING), self.read(P1_SENSOR), self.read(P2_RUNNING), self.read(P2_SENSOR)
        self.root.after(0, lambda: self.x_text.set(f"X: {x}"))
        self.root.after(0, lambda: self.y_text.set(f"Y: {y}"))
        self.root.after(0, lambda: self.vac_text.set(f"Vacuum: {'ON' if vac else 'OFF'}"))
        self.root.after(0, lambda: self.p1_text.set(f"P1: {'running' if p1r else ('occupied' if p1s else 'empty')}"))
        self.root.after(0, lambda: self.p2_text.set(f"P2: {'running' if p2r else ('occupied' if p2s else 'empty')}"))

   # The poll_status method is called periodically to check the connection status and update the live crane information. It ensures that the HMI remains responsive and provides real-time feedback to the user.
    def poll_status(self):
        if self.connected and not self.busy:
            try: self.update_live()
            except Exception as e: self.set_status(f"Status read failed: {e}")
        self.root.after(700, self.poll_status)

    # updates the list of saved crane positions in the GUI. It clears the existing entries, repopulates them with the current positions, and updates the selection based on user interaction or default behavior.
    def refresh_positions(self, selected=None):
        self.tree.delete(*self.tree.get_children())
        for name, pos in self.positions.items():
            self.tree.insert("", "end", iid=name, values=(pos["setX"], pos["setY"]))
        self.combo["values"] = list(self.positions)
        if selected in self.positions:
            self.selected_position.set(selected)
            self.tree.selection_set(selected)
        elif self.selected_position.get() not in self.positions and self.positions:
            self.selected_position.set(next(iter(self.positions)))


    # When the user selects a saved position, its name and coordinates automatically appear in the HMI fields.
    def position_selected(self, _event=None):
        sel = self.tree.selection()
        if sel:
            name = sel[0]; pos = self.positions[name]
            self.selected_position.set(name); self.position_name.set(name)
            self.target_x.set(str(pos["setX"])); self.target_y.set(str(pos["setY"]))



    # Updates the selected saved position with the crane's current X and Y coordinates, refreshes the HMI table and reminds the user to save the JSON file to keep the changes..
    def update_position(self):
        sel = self.tree.selection()
        name = sel[0] if sel else self.selected_position.get()
        if name not in self.positions:
            messagebox.showwarning("Select position", "Select a saved position first."); return
        def task():
            x, y = self.read(AT_X), self.read(AT_Y)
            self.positions[name] = {"setX": x, "setY": y}
            self.root.after(0, lambda: self.refresh_positions(name))
            self.set_status(f"Updated '{name}' to current crane position X={x}, Y={y}. Save JSON to keep it.")
        self.background(task, "Reading current crane position…")

     # The add_position method allows users to add a new saved position based on the target X and Y coordinates entered in the GUI. It validates the input, ensuring that the name is provided and that the coordinates are within acceptable bounds. If valid, it adds the new position to the internal positions dictionary and refreshes the GUI to display the updated list of saved positions.
    def add_position(self):
        name = self.position_name.get().strip()
        if not name:
            messagebox.showerror("Missing name", "Enter a name for the new position."); return
        try: x, y = int(self.target_x.get()), int(self.target_y.get())
        except ValueError:
            messagebox.showerror("Invalid position", "X and Y must be whole numbers."); return
        if not (0 <= x <= 1000 and 0 <= y <= 1000):
            messagebox.showerror("Invalid position", "X and Y must be between 0 and 1000."); return
        self.positions[name] = {"setX": x, "setY": y}
        self.refresh_positions(name)
        self.set_status(f"Added position '{name}'. Save JSON to keep it.")

    # The move_selected method moves the crane to the coordinates of the currently selected saved position. It retrieves the position's name from the selection, checks if it exists in the saved positions, and then initiates a background task to move the crane to the specified X and Y coordinates. If no valid position is selected, it prompts the user to select one first.
    def move_selected(self):
        name = self.selected_position.get()
        if name not in self.positions:
            messagebox.showwarning("Select position", "Select a saved position."); return
        p = self.positions[name]
        self.background(lambda: self.move_crane(p["setX"], p["setY"]), f"Moving to {name}…")

    # The add_move method adds a move action to the sequence based on the currently selected saved position. It retrieves the position's name, checks if it exists in the saved positions, and then appends the corresponding action to the actions list. If no valid position is selected, it prompts the user to select one first.
    def add_move(self):
        name = self.selected_position.get()
        if name in self.positions:
            self.add_action(dict(self.positions[name]))
        else:
            messagebox.showwarning("Select position", "Select a saved position.")

    def add_action(self, action):
        self.actions.append(dict(action)); self.refresh_actions()
        self.set_status(f"Added action: {action}")

    # The describe method provides a human-readable description of an action in the sequence. It checks the type of action (move, vacuum, start process) and returns a formatted string that summarizes the action's parameters. This method is used to display the actions in the GUI list for user clarity.
    @staticmethod
    def describe(action):
        if "setX" in action: return f"Move to X={action['setX']}, Y={action.get('setY', SAFE_Y)}"
        if "vacuum" in action: return f"Vacuum {'ON' if action['vacuum'] else 'OFF'}"
        if "startp1" in action: return "Start Process 1"
        if "startp2" in action: return "Start Process 2"
        return str(action)

    # The refresh_actions method updates the list of actions in the GUI. It clears the existing entries in the action list and repopulates it with the current actions, providing a numbered description for each action. This allows users to see the sequence of actions they have built and make modifications as needed.
    def refresh_actions(self):
        self.action_list.delete(0, tk.END)
        for i, a in enumerate(self.actions, 1): self.action_list.insert(tk.END, f"{i}. {self.describe(a)}")

    # The remove_action method removes the currently selected action from the sequence. It checks if an action is selected in the action list, and if so, it deletes that action from the actions list and refreshes the GUI to reflect the change. If no action is selected, it does nothing.
    def remove_action(self):
        sel = self.action_list.curselection()
        if sel:
            del self.actions[sel[0]]; self.refresh_actions()

    # The clear_actions method clears all actions from the sequence. It prompts the user for confirmation before removing all actions, ensuring that users do not accidentally delete their work. If confirmed, it clears the actions list and refreshes the GUI to show an empty sequence.
    def clear_actions(self):
        if not self.actions or messagebox.askyesno("Clear sequence", "Remove all actions?"):
            self.actions.clear(); self.refresh_actions()

    # The save_path method saves the current sequence of actions and saved positions to a specified JSON file. It constructs a dictionary containing the description, positions, and actions, and writes it to the file in a human-readable format. If the save operation is successful, it updates the status; if it fails, it shows an error message.
    def save_path(self, path):
        data = {"description": self.description.get().strip(), "positions": self.positions, "actions": self.actions}
        try:
            with open(path, "w", encoding="utf-8") as f: json.dump(data, f, indent=2)
            self.set_status(f"Saved {len(self.positions)} positions and {len(self.actions)} actions to {path}")
        except Exception as e: messagebox.showerror("Save failed", str(e))

    # The choose_save method opens a file dialog for the user to select a location and filename to save the current sequence as a JSON file. It sets a default filename and filters for JSON files. If the user selects a file, it calls the save_path method to perform the save operation.
    def choose_save(self):
        p = filedialog.asksaveasfilename(defaultextension=".json", initialfile=JSON_FILE, filetypes=[("JSON files", "*.json")])
        if p: self.save_path(p)

    # The load_path method loads a sequence of actions and saved positions from a specified JSON file. It reads the file, parses the JSON data, and updates the internal positions and actions lists. It also handles compatibility with older JSON formats and provides user feedback through message boxes. If the load operation is successful, it refreshes the GUI to reflect the loaded data; if it fails, it shows an error message.
    def load_path(self, path, show):
        if not os.path.exists(path):
            if show: messagebox.showinfo("File not found", f"{path} does not exist yet. Build a sequence and save it.")
            return
        try:
            with open(path, encoding="utf-8") as f: data = json.load(f)
            if isinstance(data.get("positions"), dict):
                for name, p in data["positions"].items():
                    if isinstance(p, dict) and "setX" in p and "setY" in p:
                        self.positions[name] = {"setX": int(p["setX"]), "setY": int(p["setY"])}
            if isinstance(data.get("actions"), list):
                self.actions = [dict(a) for a in data["actions"] if isinstance(a, dict)]
            elif "type1_actions" in data or "type2_actions" in data:
                if show and messagebox.askyesno("Import Grade B JSON", "Import the old Grade B Type 1 actions followed by Type 2 actions into one editable sequence?"):
                    self.actions = [dict(a) for a in data.get("type1_actions", []) + data.get("type2_actions", []) if isinstance(a, dict)]
            else: self.actions = []
            self.description.set(data.get("description", "Grade A sequence created with the HMI"))
            self.refresh_positions(); self.refresh_actions()
            if show: self.set_status(f"Loaded {path}")
        except Exception as e: messagebox.showerror("Load failed", f"Could not load JSON:\n{e}")

    # The choose_load method opens a file dialog for the user to select a JSON file to load a sequence of actions and saved positions. It filters for JSON files and, if the user selects a file, it calls the load_path method to perform the load operation.
    def choose_load(self):
        p = filedialog.askopenfilename(filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
        if p: self.load_path(p, True)

    # The execute method initiates the execution of the current sequence of actions. It checks if the HMI is connected to the simulator and if there are any actions to execute. If both conditions are met, it prompts the user for confirmation before proceeding. The actions are then executed in a background thread to keep the GUI responsive, and the status is updated throughout the execution process.
    def execute(self):
        if not self.connected:
            messagebox.showwarning("Not connected", "Connect to the simulator first."); return
        if not self.actions:
            messagebox.showwarning("Empty sequence", "Add actions first."); return
        if not messagebox.askyesno("Execute sequence", f"Execute {len(self.actions)} actions?\nStop crane_controller.py first to avoid two programs controlling the crane."):
            return
        actions = [dict(a) for a in self.actions]
        self.background(lambda: self.execute_actions(actions), "Executing sequence…")


    # The execute_actions method processes a list of actions sequentially. For each action, it updates the status to indicate which action is being executed and performs the corresponding Modbus operations (moving the crane, controlling the vacuum, or starting processes). It handles errors by raising exceptions for unsupported actions or invalid parameters. After all actions are executed, it updates the status to indicate completion.
    def execute_actions(self, actions):
        for i, a in enumerate(actions, 1):
            self.set_status(f"Executing {i}/{len(actions)}: {self.describe(a)}")
            if "setX" in a: self.move_crane(a["setX"], a.get("setY", SAFE_Y))
            elif "vacuum" in a:
                if int(a["vacuum"]) not in (0, 1): raise ValueError("Vacuum must be 0 or 1.")
                self.write(VACUUM, int(a["vacuum"])); time.sleep(0.1)
            elif "startp1" in a: self.write(START_P1, int(a["startp1"]))
            elif "startp2" in a: self.write(START_P2, int(a["startp2"]))
            else: raise ValueError(f"Unsupported action: {a}")
        self.set_status("Sequence completed.")


    # The background method runs a given function in a separate thread to keep the GUI responsive. It uses a threading lock to prevent multiple operations from running simultaneously. If the lock is already acquired, it shows a warning message. After the function completes, it releases the lock and updates the live status if connected.
    def background(self, fn, message=None):
        if not self.lock.acquire(blocking=False):
            messagebox.showwarning("Busy", "Wait for the current crane operation to finish."); return
        self.busy = True
        if message: self.set_status(message)
        def worker():
            try: fn()
            except Exception as e:
                self.set_status(f"Error: {e}")
                self.root.after(0, lambda msg=str(e): messagebox.showerror("Operation failed", msg))
            finally:
                self.busy = False; self.lock.release()
                if self.connected:
                    try: self.update_live()
                    except Exception: pass
        threading.Thread(target=worker, daemon=True).start()
    # The set_status method updates the status message displayed in the HMI. It uses the Tkinter after method to ensure that the update occurs in the main GUI thread, preventing potential threading issues.
    def set_status(self, msg):
        self.root.after(0, lambda m=msg: self.status.set(m))

    # The close method attempts to close the Modbus client connection and then destroys the main Tkinter window, effectively shutting down the HMI application. It handles any exceptions that may occur during the client closure to ensure a graceful exit.
    def close(self):
        try: self.client.close()
        except Exception: pass
        self.root.destroy()

 # Starts the Tkinter HMI application, creates the main window, initializes the CraneHMI interface and runs the GUI event loop to keep the application open and responsive.
if __name__ == "__main__":
    root = tk.Tk()
    app = CraneHMI(root)
    root.mainloop()
