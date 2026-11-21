# This file is responsible for actually controlling the crane.
# ===============================================================================================
#-------------------------------1. Modbus connection--------------------------------
# The controller connects to the simulator using the Modbus TCP client.


# ===============================================================================================
#------------------------------ 2. Reading and writing registers--------------------------------
# We created functionality to:
# - Write values to Modbus registers.
# - Read values from Modbus registers.
#- Check whether operations are successful.


# ===============================================================================================
#-------------------------------3. Reading the JSON file--------------------------------
# The controller reads crane_sequence.json to get the instructions for Type 1 and Type 2 parts.

import time
import csv
import os
from datetime import datetime
from pymodbus.client import ModbusTcpClient # connect our Python controller to the simulator through Modbus TCP.

# Output addresses (Modbus register addresses used to send commands TO the simulator.)
SET_X = 1
SET_Y = 2
VACUUM = 3
START_P1 = 4
START_P2 = 5

# Input addresses (Modbus register addresses used to read status FROM the simulator.)
AT_X = 15
AT_Y = 16
SOURCE1 = 17
SOURCE2 = 18
P1_RUNNING = 19
P2_RUNNING = 20
P1_SENSOR = 21
P2_SENSOR = 22

# Station positions
SOURCE1_X = 55
SOURCE2_X = 158
PROCESS1_X = 450
PROCESS2_X = 650
SINK_X = 945
PICK_Y = 82
SAFE_Y = 200

TIMEOUT = 20
LOG_FILE = "product_log.csv"
POLL_INTERVAL = 0.05

client = ModbusTcpClient("127.0.0.1", port=502)

jobs = []   # will store detected products waiting to be handled
next_product_id = 0 # will be incremented for each new product detected
previous_source1 = 0  # Stores the previous status of Source 1.
previous_source2 = 0  # Stores the previous status of Source 2.


# function sends a value to a simulator and checks if the operation was successful
def write_register(address, value):
    response = client.write_register(address=address, value=value)
    if response.isError():
        raise RuntimeError(f"Failed to write register {address}")
    

# function reads a value from a simulator and checks if the operation was successful
def read_register(address):
    response = client.read_holding_registers(address=address, count=1)
    if response.isError():
        raise RuntimeError(f"Failed to read register {address}")
    return response.registers[0]

# Scan both sources to detect new parts repeatedly
def scan_sources():
    """Queue new parts in the order their source sensors are observed."""
    global previous_source1, previous_source2, next_product_id

    source1 = read_register(SOURCE1)
    source2 = read_register(SOURCE2)

    # If both become occupied between scans, S1 is the deterministic tie-breaker.
    if source1 and not previous_source1:
        next_product_id += 1
        # adds a new product record to the jobs list.
        jobs.append({
            "id": next_product_id,
            "type": 1,
            "arrival_order": next_product_id,
            "phase": "PICK_SOURCE",
            "carrying": False,
            "last_position": None
        })
        print(f"New Type 1 part detected at Source 1. Product ID: {next_product_id}")

# If a new part is detected at Source 2, it is added to the jobs list with type 2.
    if source2 and not previous_source2:
        next_product_id += 1
        jobs.append({
            "id": next_product_id,
            "type": 2,
            "arrival_order": next_product_id,
            "phase": "PICK_SOURCE",
            "carrying": False,
            "last_position": None
        })
        print(f"New Type 2 part detected at Source 2. Product ID: {next_product_id}")

# Update the previous source states for the next scan.
    previous_source1 = source1
    previous_source2 = source2

# function to make the controller wait until the crane reaches its requested position in meantime 
# continues to scan the sources for new parts and checks for timeout
def wait_for_position(address, target):
    start_time = time.time()
    while read_register(address) != target:
        scan_sources()
        if time.time() - start_time > TIMEOUT:
            raise TimeoutError(f"Crane did not reach position {target}")
        time.sleep(POLL_INTERVAL)
    scan_sources()

# function to log the crane's position while carrying a part
def log_location(job, x, y):
    """log new positions only while carrying."""
    if read_register(VACUUM) != 1:
        return
# log the position only if it has changed since the last log entry for this job
    position = (x, y)
    if position == job["last_position"]:
        return

# log the product's position to a CSV file with a timestamp.
# verify if the log file exists and write the header if it doesn't.
    timestamp = datetime.now().isoformat(timespec="seconds")
    file_exists = os.path.exists(LOG_FILE)

    with open(LOG_FILE, "a", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        if not file_exists or os.path.getsize(LOG_FILE) == 0:
            writer.writerow(["product_id", "timestamp", "x", "y", "vacuum"])
        writer.writerow([job["id"], timestamp, x, y, 1])

    job["last_position"] = position
    print(f"Logged product {job['id']}: X={x}, Y={y}, vacuum=1")

# handles the complete movement operation for picking up or placing a product at any station.
def move_to_station(job, target_x, pickup):
    """Move vertically to safe height, move horizontally, then lower."""
    write_register(SET_Y, SAFE_Y)
    wait_for_position(AT_Y, SAFE_Y)
    log_location(job, read_register(AT_X), SAFE_Y)

    write_register(SET_X, target_x)
    wait_for_position(AT_X, target_x)
    log_location(job, target_x, SAFE_Y)

    write_register(SET_Y, PICK_Y)
    wait_for_position(AT_Y, PICK_Y)
    log_location(job, target_x, PICK_Y)

    if pickup:
        # Do not pick from an empty source or process station.
        write_register(VACUUM, 1)
        time.sleep(0.1)
        job["carrying"] = True
        log_location(job, target_x, PICK_Y)
        print(f"Product {job['id']} picked up at X={target_x}")

       # After picking up, move back to the safe height before moving horizontally.
        write_register(SET_Y, SAFE_Y)
        wait_for_position(AT_Y, SAFE_Y)
        log_location(job, target_x, SAFE_Y)
    else:
        write_register(VACUUM, 0)
        time.sleep(0.1)
        job["carrying"] = False
        print(f"Product {job['id']} placed at X={target_x}")

        write_register(SET_Y, SAFE_Y)
        wait_for_position(AT_Y, SAFE_Y)

# function to check if a process is available for a new part to be placed or picked up.
def process_available(running_address, sensor_address):
    return read_register(running_address) == 0 and read_register(sensor_address) == 0

# function to check if a process has finished and promote the part to the next phase.
def refresh_finished_processes():
    """Promote completed parts to their next phase as soon as the signals allow."""
    for job in jobs:
        if job["phase"] == "WAIT_P1":
            if read_register(P1_RUNNING) == 0 and read_register(P1_SENSOR) == 1:
                job["phase"] = "PICK_P1"
                print(f"Product {job['id']}: Process 1 finished; ready for sink")

        elif job["phase"] == "WAIT_P2":
            if read_register(P2_RUNNING) == 0 and read_register(P2_SENSOR) == 1:
                job["phase"] = "PICK_P2"
                print(f"Product {job['id']}: Process 2 finished; ready to move to Process 1")

# function to check if an older Type 2 part is blocking a newer source job from starting.
def older_type2_blocks_later_source_jobs(job):
    """
    If an older Type 2 part is in/at Process 2, do not start handling a newer
    source part ahead of it. This prevents a source pickup from beginning just
    before the older P2 part finishes and then accidentally jumping the queue.
    """
    for other in jobs:
        if other["phase"] == "DONE" or other["id"] == job["id"]:
            continue
        if other["type"] == 2 and other["arrival_order"] < job["arrival_order"]:
            if other["phase"] in ("PLACE_P2", "START_P2", "WAIT_P2", "PICK_P2", "PLACE_P1"):
                return True
    return False

# controls the different stages of a product's journey through the factory based on its current phase and type
def run_job_step(job):
    phase = job["phase"]
    product_type = job["type"]

    if phase == "PICK_SOURCE":
        # Do not let a later source part jump ahead of an older Type 2 part
        # that still needs to move from P2 through P1.
        if older_type2_blocks_later_source_jobs(job):
            return False
        
        if product_type == 1:
            if not process_available(P1_RUNNING, P1_SENSOR):
                return False
            # Check the source again immediately before moving to pick it.
            if read_register(SOURCE1) == 0:
                job["phase"] = "DONE"
                return True
            move_to_station(job, SOURCE1_X, pickup=True)
            job["phase"] = "PLACE_P1"

        elif product_type == 2:
            if not process_available(P2_RUNNING, P2_SENSOR):
                return False
            if read_register(SOURCE2) == 0:
                job["phase"] = "DONE"
                return True
            move_to_station(job, SOURCE2_X, pickup=True)
            job["phase"] = "PLACE_P2"
        return True
    # Moves the product to Process 1 and places it there.
    if phase == "PLACE_P1":
        if product_type not in (1, 2):
            raise RuntimeError(f"Invalid product type for Process 1: {product_type}")
        if not job["carrying"]:
            raise RuntimeError(f"Product {job['id']} is not being carried for Process 1 placement")
        if not process_available(P1_RUNNING, P1_SENSOR):
            return False
        move_to_station(job, PROCESS1_X, pickup=False)
        job["phase"] = "START_P1"
        return True
    
    # Moves a Type 2 product to Process 2 and places it there.
    if phase == "PLACE_P2":
        if product_type != 2:
            raise RuntimeError(f"SAFETY STOP: Type {product_type} cannot enter Process 2")
        if not job["carrying"]:
            raise RuntimeError(f"Product {job['id']} is not being carried for Process 2 placement")
        if not process_available(P2_RUNNING, P2_SENSOR):
            return False
        move_to_station(job, PROCESS2_X, pickup=False)
        job["phase"] = "START_P2"
        return True
    # Checks that a product is present, starts Process 1 and changes phase to WAIT_P1.
    if phase == "START_P1":
        if read_register(P1_SENSOR) == 0:
            raise RuntimeError(f"Safety stop: Process 1 has no part for product {job['id']}")
        write_register(START_P1, 1)
        job["phase"] = "WAIT_P1"
        print(f"Product {job['id']}: Process 1 started")
        return True
    # Checks that a Type 2 product is present, starts Process 2 and changes phase to WAIT_P2.
    if phase == "START_P2":
        if product_type != 2:
            raise RuntimeError(f"SAFETY STOP: Type {product_type} cannot start Process 2")
        if read_register(P2_SENSOR) == 0:
            raise RuntimeError(f"Safety stop: Process 2 has no part for product {job['id']}")
        write_register(START_P2, 1)
        job["phase"] = "WAIT_P2"
        print(f"Product {job['id']}: Process 2 started")
        return True 
    # Once Process 2 finishes, picks up the Type 2 product and moves it towards Process 1.
    if phase == "PICK_P2":
        if product_type != 2:
            raise RuntimeError(f"SAFETY STOP: non-Type-2 product {job['id']} reached PICK_P2")
        if not process_available(P1_RUNNING, P1_SENSOR):
            return False
        if read_register(P2_RUNNING) != 0 or read_register(P2_SENSOR) == 0:
            return False
        move_to_station(job, PROCESS2_X, pickup=True)
        job["phase"] = "PLACE_P1"
        return True
    # Once Process 1 finishes, picks up the product for delivery.
    if phase == "PICK_P1":
        if read_register(P1_RUNNING) != 0 or read_register(P1_SENSOR) == 0:
            return False
        move_to_station(job, PROCESS1_X, pickup=True)
        job["phase"] = "DELIVER"
        return True
    # Moves the product to the sink, releases it and marks the job as DONE.
    if phase == "DELIVER":
        if not job["carrying"]:
            raise RuntimeError(f"Product {job['id']} is not being carried for delivery")
        move_to_station(job, SINK_X, pickup=False)
        job["phase"] = "DONE"
        print(f"Product {job['id']} delivered to Sink")
        return True

    return False

# function to choose the next job based on scheduling rules and current state of jobs
def choose_next_job():
    """
    Scheduling rules:
    1. Never abandon a part already in the gripper.
    2. Move finished P1 parts to the sink.
    3. Move a finished P2 part to P1 before newer source jobs.
    4. Otherwise, choose the oldest eligible source/job in arrival order.
    """
    active = sorted((j for j in jobs if j["phase"] != "DONE"),
                    key=lambda j: j["arrival_order"])

    carrying = [j for j in active if j["carrying"]]
    if carrying:
        return carrying[0]

    # Start processing immediately after a part has been placed.
    # Do this before collecting completed parts, so a newly placed part at
    # P2 does not wait for the crane to deliver a finished P1 part first.
    for job in active:
        if job["phase"] in ("START_P1", "START_P2"):
            return job

    # P1 must be cleared before another part can be placed there.
    for job in active:
        if job["phase"] == "PICK_P1":
            return job

    # P2->P1 transfer has priority over any later-arriving source part.
    for job in active:
        if job["phase"] == "PICK_P2":
            return job

    # If an older Type 2 part is still processing at P2, don't start a newer
    # source pickup that could occupy the crane and delay the P2->P1 transfer.
    for job in active:
        if job["phase"] == "PICK_SOURCE" and older_type2_blocks_later_source_jobs(job):
            continue
        if job["phase"] not in ("WAIT_P1", "WAIT_P2"):
            return job

    return None


try:
    if not client.connect():
        raise ConnectionError("Could not connect to the simulation.")

    print("Connected to the crane simulation.")
    print("Part 1 and Part 2 complete controller is running.")
    print("Parts are scheduled by observed arrival order.")
    print("Controller keeps running until Ctrl+C.")
    scan_sources()

    while True:
        scan_sources()
        refresh_finished_processes()

        job = choose_next_job()
        if job is not None:
            made_progress = run_job_step(job)
            if not made_progress:
                time.sleep(POLL_INTERVAL)
        else:
            time.sleep(POLL_INTERVAL)

except KeyboardInterrupt:
    print("\nController stopped by user.")

except Exception as error:
    print("Error:", error)

finally:
    client.close()
    print("Connection closed.")
