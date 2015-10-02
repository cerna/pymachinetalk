import time
import uuid
import platform

import zmq
import threading

# protobuf
from message_pb2 import Container
from types_pb2 import *
from status_pb2 import *


class ApplicationStatus():

    def __init__(self, debug=False):
        self.threads = []
        self.shutdown = threading.Event()
        self.config_lock = threading.Lock()
        self.io_lock = threading.Lock()
        self.motion_lock = threading.Lock()
        self.task_lock = threading.Lock()
        self.interp_lock = threading.Lock()
        self.debug = debug
        self.is_ready = False

        self.synced = False
        self.connected = False
        self.state = 'Disconnected'
        self.status_state = 'Down'
        self.channels = set(['motion', 'config', 'io', 'task', 'interp'])
        self.running = False

        # status containers, also used to expose data
        self._io = EmcStatusIo()
        self._config = EmcStatusConfig()
        self._motion = EmcStatusMotion()
        self._task = EmcStatusTask()
        self._interp = EmcStatusInterp()

        self.status_uri = ''
        self.status_period = 0
        self.status_timer = None
        self.subscriptions = set()
        self.synced_channels = set()

        # more efficient to reuse a protobuf message
        self.rx = Container()

        # ZeroMQ
        context = zmq.Context()
        context.linger = 0
        self.context = context
        self.status_socket = self.context.socket(zmq.SUB)
        self.sockets_connected = False

    # make sure locks are used when accessing properties
    # should we return a copy instead of the reference?
    @property
    def io(self):
        with self.io_lock:
            return self._io

    @property
    def config(self):
        with self.config_lock:
            return self._config

    @property
    def motion(self):
        with self.motion_lock:
            return self._motion

    @property
    def task(self):
        with self.task_lock:
            return self._task

    @property
    def interp(self):
        with self.interp_lock:
            return self._interp

    def socket_worker(self):
        poll = zmq.Poller()
        poll.register(self.status_socket, zmq.POLLIN)

        while not self.shutdown.is_set():
            s = dict(poll.poll(200))
            if self.status_socket in s:
                self.process_status()

    def process_status(self):
        (topic, msg) = self.status_socket.recv_multipart()
        self.rx.ParseFromString(msg)

        if self.debug:
            print('[status] received message: %s' % topic)
            print(self.rx)

        if self.rx.type == MT_EMCSTAT_FULL_UPDATE \
           or self.rx.type == MT_EMCSTAT_INCREMENTAL_UPDATE:

            if topic == 'motion' and self.rx.HasField('emc_status_motion'):
                self.update_motion(self.rx.emc_status_motion)
                if self.rx.type == MT_EMCSTAT_FULL_UPDATE:
                    self.update_sync('motion')

            if topic == 'config' and self.rx.HasField('emc_status_config'):
                self.update_config(self.rx.emc_status_config)
                if self.rx.type == MT_EMCSTAT_FULL_UPDATE:
                    self.update_sync('config')

            if topic == 'io' and self.rx.HasField('emc_status_io'):
                self.update_io(self.rx.emc_status_io)
                if self.rx.type == MT_EMCSTAT_FULL_UPDATE:
                    self.update_sync('io')

            if topic == 'task' and self.rx.HasField('emc_status_task'):
                self.update_task(self.rx.emc_status_task)
                if self.rx.type == MT_EMCSTAT_FULL_UPDATE:
                    self.update_sync('task')

            if topic == 'interp' and self.rx.HasField('emc_status_interp'):
                self.update_interp(self.rx.emc_status_interp)
                if self.rx.type == MT_EMCSTAT_FULL_UPDATE:
                    self.update_sync('interp')

            if self.rx.type == MT_EMCSTAT_FULL_UPDATE:
                if not self.status_state == 'Up':
                    self.status_state = 'Up'
                    self.update_state('Connected')

                if self.rx.HasField('pparams'):
                    interval = self.rx.pparams.keepalive_timer
                    self.start_status_heartbeat(interval * 2)  # wait double the hearbeat intverval
            else:
                self.refresh_status_heartbeat()

        elif self.rx.type == MT_PING:
            if self.status_state == 'Up':
                self.refresh_status_heartbeat()
            else:
                self.update_state('Connecting')
                self.unsubscribe()  # clean up previous subscription
                self.subscribe()  # trigger a fresh subscribe -> full update
        else:
            print('[status] received unrecognized message type')

    def update_motion(self, data):
        with self.motion_lock:
            self._motion.MergeFrom(data)

    def update_config(self, data):
        with self.config_lock:
            self._config.MergeFrom(data)

    def update_io(self, data):
        with self.io_lock:
            self._io.MergeFrom(data)

    def update_task(self, data):
        with self.task_lock:
            self._task.MergeFrom(data)
            self.update_running()

    def update_interp(self, data):
        with self.interp_lock:
            self._interp.MergeFrom(data)
            self.update_running()

    def update_sync(self, channel):
        self.synced_channels.add(channel)

        if self.synced_channels == self.channels:
            self.synced = True

    def clear_sync(self):
        self.synced = False
        self.synced_channels.clear()

    def status_timer_tick(self):
        self.status_state = 'Down'
        self.update_state('Timeout')

    def start_status_heartbeat(self, interval):
        if self.status_timer:
            self.status_timer.cancel()

        self.status_period = interval
        if interval > 0:
            self.status_timer = threading.Timer(interval / 1000,
                                                self.status_timer_tick)
            self.status_timer.start()

    def refresh_status_heartbeat(self):
        if self.status_timer:
            self.status_timer.cancel()
            self.status_timer = threading.Timer(self.status_period / 1000,
                                                self.status_timer_tick)
            self.status_timer.start()

    def stop_status_heartbeat(self):
        if self.status_timer:
            self.status_timer.cancel()
            self.status_timer = None

    def update_state(self, state):
        if state != self.state:
            self.state = state
            if state == 'Connected':
                self.connected = True
                print('[status] connected')
            elif self.connected:
                self.connected = False
                self.stop_status_heartbeat()
                self.clear_sync()
                self.status_period = 0  # stop heartbeat
                if not state == 'Timeout':  # clear in case we have no timeout
                    with self.motion_lock:
                        self._motion.Clear()
                    with self.config_lock:
                        self._config.Clear()
                    with self.io_lock:
                        self._io.Clear()
                    with self.task_lock:
                        self._task.Clear()
                    with self.interp_lock:
                        self._interp.Clear()
                print('[status] disconnected')

    def subscribe(self):
        self.status_state = 'Trying'

        for channel in self.channels:
            self.status_socket.setsockopt(zmq.SUBSCRIBE, channel)
            self.subscriptions.add(channel)

    def unsubscribe(self):
        self.status_state = 'Down'

        for subscription in self.subscriptions:
            self.status_socket.setsockopt(zmq.UNSUBSCRIBE, subscription)
            if subscription == 'motion':
                with self.motion_lock:
                    self._motion.Clear()
            elif subscription == 'config':
                with self.config_lock:
                    self._config.Clear()
            elif subscription == 'io':
                with self.io_lock:
                    self._io.Clear()
            elif subscription == 'task':
                with self.task_lock:
                    self._task.Clear()
            elif subscription == 'interp':
                with self.interp_lock:
                    self._interp.Clear()

        self.subscriptions.clear()

    def update_running(self):
        running = (self._task.task_mode == EMC_TASK_MODE_AUTO \
                   or self._task.task_mode == EMC_TASK_MODE_MDI) \
                   and self._interp.interp_state == EMC_TASK_INTERP_IDLE

        self.running = running

    def start(self):
        self.status_state = 'Trying'
        self.update_state('Connecting')

        if self.connect_sockets():
            self.shutdown.clear()  # in case we already used the component
            self.threads.append(threading.Thread(target=self.socket_worker))
            for thread in self.threads:
                thread.start()
            self.subscribe()

    def stop(self):
        self.is_ready = False
        self.shutdown.set()
        for thread in self.threads:
            thread.join()
        self.threads = []
        self.cleanup()
        self.update_state('Disconnected')

    def cleanup(self):
        if self.connected:
            self.unsubscribe()
        self.disconnect_sockets()
        self.subscriptions.clear()

    def connect_sockets(self):
        self.sockets_connected = True
        self.status_socket.connect(self.status_uri)

        return True

    def disconnect_sockets(self):
        if self.sockets_connected:
            self.status_socket.disconnect(self.status_uri)
            self.sockets_connected = False

    def ready(self):
        if not self.is_ready:
            self.is_ready = True
            self.start()


class ApplicationCommand():
    RELEASE_BRAKE = 0
    ENGAGE_BRAKE = 1

    STOP_JOG = 0
    CONTINOUS_JOG = 1
    INCREMENT_JOG = 2

    SPINDLE_FORWARD = 0
    SPINDLE_REVERSE = 1
    SPINDLE_OFF = 2
    SPINDLE_DECREASE = 3
    SPINDLE_INCREASE = 4
    SPINDLE_CONSTANT = 5

    TASK_STATE_ESTOP = EMC_TASK_STATE_ESTOP
    TASK_STATE_ESTOP_RESET = EMC_TASK_STATE_ESTOP_RESET
    TASK_STATE_OFF = EMC_TASK_STATE_OFF
    TASK_STATE_ON = EMC_TASK_STATE_ON

    TASK_MODE_MANUAL = EMC_TASK_MODE_MANUAL
    TASK_MODE_AUTO = EMC_TASK_MODE_AUTO
    TASK_MODE_MDI = EMC_TASK_MODE_MDI

    def __init__(self, debug=False):
        self.threads = []
        self.shutdown_event = threading.Event()
        self.tx_lock = threading.Lock()
        self.debug = debug
        self.is_ready = False

        self.connected = False
        self.state = 'Disconnected'
        self.command_state = 'Down'

        self.command_uri = ''
        self.heartbeat_period = 3000
        self.ping_error_count = 0
        self.ping_error_threshold = 2
        self.heartbeat_timer = None

        # more efficient to reuse a protobuf message
        self.rx = Container()
        self.tx = Container()

        # ZeroMQ
        client_id = '%s-%s' % (platform.node(), uuid.uuid4())  # must be unique
        context = zmq.Context()
        context.linger = 0
        self.context = context
        self.command_socket = self.context.socket(zmq.DEALER)
        self.command_socket.setsockopt(zmq.LINGER, 0)
        self.command_socket.setsockopt(zmq.IDENTITY, client_id)
        self.sockets_connected = False

    def send_command_msg(self, msg_type):
        self.tx.type = msg_type
        if self.debug:
            print('[command] sending message: %s' % msg_type)
            print(str(self.tx))
        self.command_socket.send(self.tx.SerializeToString(), zmq.NOBLOCK)
        self.tx.Clear()

    def socket_worker(self):
        poll = zmq.Poller()
        poll.register(self.command_socket, zmq.POLLIN)

        while not self.shutdown_event.is_set():
            s = dict(poll.poll(200))
            if self.command_socket in s:
                self.process_command()

    def process_command(self):
        msg = self.command_socket.recv()
        self.rx.ParseFromString(msg)
        if self.debug:
            print('[command] received message')
            print(self.rx)

        if self.rx.type == MT_PING_ACKNOWLEDGE:
            self.ping_error_count = 0

            if not self.command_state == 'Up':
                self.command_state = 'Up'
                self.update_state('Connected')

            elif self.rx.type == MT_ERROR:
                self.update_error('Service', self.rx.note)
                # should we disconnect here?

            else:
                print('[command] received unsupported message')

    def start(self):
        self.command_state = 'Trying'
        self.update_state('Connecting')

        if self.connect_sockets():
            self.shutdown_event.clear()  # in case we already used the component
            self.threads.append(threading.Thread(target=self.socket_worker))
            for thread in self.threads:
                thread.start()
            self.start_command_heartbeat()
            with self.tx_lock:
                self.send_command_msg(MT_PING)

    def stop(self):
        self.is_ready = False
        self.shutdown_event.set()
        for thread in self.threads:
            thread.join()
        self.threads = []
        self.cleanup()
        self.update_state('Disconnected')

    def cleanup(self):
        self.stop_command_heartbeat()
        self.disconnect_sockets()

    def connect_sockets(self):
        self.sockets_connected = True
        self.command_socket.connect(self.command_uri)

        return True

    def disconnect_sockets(self):
        if self.sockets_connected:
            self.command_socket.disconnect(self.command_uri)
            self.sockets_connected = False

    def ready(self):
        if not self.is_ready:
            self.is_ready = True
            self.start()

    def update_state(self, state):
        if state != self.state:
            self.state = state
            if state == 'Connected':
                self.connected = True
                print('[command] connected')
            elif self.connected:
                self.connected = False
                print('[command] disconnected')

    def update_error(self, error, description):
        print('[command] error: %s %s' % (error, description))

    def heartbeat_timer_tick(self):
        self.ping_error_count += 1  # increase error count by one, threshold 2 means two timer ticks

        if self.ping_error_count > self.ping_error_threshold:
            self.command_state = 'Trying'
            self.update_state('Timeout')

        with self.tx_lock:
            self.send_command_msg(MT_PING)

        self.heartbeat_timer = threading.Timer(self.heartbeat_period / 1000,
                                             self.heartbeat_timer_tick)
        self.heartbeat_timer.start()  # rearm timer

    def start_command_heartbeat(self):
        if not self.connected:
            return

        self.ping_error_count = 0  # reset heartbeat

        if self.heartbeat_period > 0:
            self.heartbeat_timer = threading.Timer(self.heartbeat_period / 1000,
                                                 self.heartbeat_timer_tick)
            self.heartbeat_timer.start()

    def stop_command_heartbeat(self):
        if self.heartbeat_timer:
            self.heartbeat_timer.cancel()
            self.heartbeat_timer = None

    def abort(self, interpreter):
        if not self.connected:
            return

        with self.tx_lock:
            self.tx.interp_name = interpreter

            self.send_command_msg(MT_EMC_TASK_ABORT)

    def run_program(self, interpreter, line_number):
        if not self.connected:
            return

        with self.tx_lock:
            params = self.tx.emc_command_params
            params.line_number = line_number
            self.tx.interp_name = interpreter

            self.send_command_msg(MT_EMC_TASK_PLAN_RUN)

    def pause_program(self, interpreter):
        if not self.connected:
            return

        with self.tx_lock:
            self.tx.interp_name = interpreter

            self.send_command_msg(MT_EMC_TASK_PLAN_PAUSE)

    def step_program(self, interpreter):
        if not self.connected:
            return

        with self.tx_lock:
            self.tx.interp_name = interpreter

            self.send_command_msg(MT_EMC_TASK_PLAN_STEP)

    def resume_program(self, interpreter):
        if not self.connected:
            return

        with self.tx_lock:
            self.tx.interp_name = interpreter

            self.send_command_msg(MT_EMC_TASK_RESUME)

    def reset_program(self, interpreter):
        if not self.connected:
            return

        with self.tx_lock:
            self.tx.interp_name = interpreter

            self.send_command_msg(MT_EMC_TASK_PLAN_INIT)

    def set_task_mode(self, interpreter, mode):
        if not self.connected:
            return

        with self.tx_lock:
            params = self.tx.emc_command_params
            params.task_mode = mode
            self.tx.interp_name = interpreter

            self.send_command_msg(MT_EMC_TASK_SET_MODE)

    def set_task_state(self, interpreter, state):
        if not self.connected:
            return

        with self.tx_lock:
            params = self.tx.emc_command_params
            params.task_state = state
            self.tx.interp_name = interpreter

            self.send_command_msg(MT_EMC_TASK_SET_STATE)

    def open_program(self, interpreter, file_name):
        if not self.connected:
            return

        with self.tx_lock:
            params = self.tx.emc_command_params
            params.path = file_name
            self.tx.interp_name = interpreter

            self.send_command_msg(MT_EMC_TASK_PLAN_OPEN)

    def execute_mdi(self, interpreter, command):
        if not self.connected:
            return

        with self.tx_lock:
            params = self.tx.emc_command_params
            params.command = command
            self.tx.interp_name = interpreter

            self.send_command_msg(MT_EMC_TASK_PLAN_EXECUTE)

    def set_spindle_brake(self, brake):
        if not self.connected:
            return

        with self.tx_lock:
            if brake == self.ENGAGE_BRAKE:
                self.send_command_msg(MT_EMC_SPINDLE_BRAKE_ENGAGE)
            elif brake == self.RELEASE_BRAKE:
                self.send_command_msg(MT_EMC_SPINDLE_BRAKE_RELEASE)

    def set_debug_level(self, debug_level):
        if not self.connected:
            return

        with self.tx_lock:
            params = self.tx.emc_command_params
            params.debug_level = debug_level
            self.tx.interp_name = debug_level

            self.send_command_msg(MT_EMC_SET_DEBUG)

    def set_feed_override(self, scale):
        if not self.connected:
            return

        with self.tx_lock:
            params = self.tx.emc_command_params
            params.scale = scale

            self.send_command_msg(MT_EMC_TRAJ_SET_SCALE)

    def set_flood_enabled(self, enable):
        if not self.connected:
            return

        with self.tx_lock:
            if enable:
                self.send_command_msg(MT_EMC_COOLANT_FLOOD_ON)
            else:
                self.send_command_msg(MT_EMC_COOLANT_FLOOD_OFF)

    def home_axis(self, index):
        if not self.connected:
            return

        with self.tx_lock:
            params = self.tx.emc_command_params
            params.index = index

            self.send_command_msg(MT_EMC_AXIS_HOME)

    def jog(self, jog_type, axis, velocity=0.0, distance=0.0):
        if not self.connected:
            return

        with self.tx_lock:
            params = self.tx.emc_command_params
            params.index = axis

            cmd_type = None
            if jog_type == self.STOP_JOG:
                cmd_type = MT_EMC_AXIS_ABORT
            elif jog_type == self.CONTINOUS_JOG:
                cmd_type = MT_EMC_AXIS_JOG
                params.velocity = velocity
            elif jog_type == self.INCREMENT_JOG:
                cmd_type = MT_EMC_AXIS_INCR_JOG
                params.velocity = velocity
                params.distance = distance
            else:
                self.tx.Clear()
                return

            self.send_command_msg(cmd_type)

    def load_tool_table(self):
        if not self.connected:
            return

        with self.tx_lock:
            self.send_command_msg(MT_EMC_TOOL_LOAD_TOOL_TABLE)

    def set_maximum_velocity(self, velocity):
        if not self.connected:
            return

        with self.tx_lock:
            params = self.tx.emc_command_params
            params.velocity = velocity

            self.send_command_msg(MT_EMC_TRAJ_SET_MAX_VELOCITY)

    def set_mist_enabled(self, enable):
        if not self.connected:
            return

        with self.tx_lock:
            if enable:
                self.send_command_msg(MT_EMC_COOLANT_MIST_ON)
            else:
                self.send_command_msg(MT_EMC_COOLANT_MIST_OFF)

    def override_limits(self):
        if not self.connected:
            return

        with self.tx_lock:
            self.send_command_msg(MT_EMC_AXIS_OVERRIDE_LIMITS)

    def set_adaptive_feed_enabled(self, enable):
        if not self.connected:
            return

        with self.tx_lock:
            params = self.tx.emc_command_params
            params.enable = enable

            self.send_command_msg(MT_EMC_MOTION_ADAPTIVE)

    def set_analog_output(self, index, value):
        if not self.connected:
            return

        with self.tx_lock:
            params = self.tx.emc_command_params
            params.index = index
            params.value = value

            self.send_command_msg(MT_EMC_MOTION_SET_AOUT)

    def set_block_delete_enabled(self, enable):
        if not self.connected:
            return

        with self.tx_lock:
            params = self.tx.emc_command_params
            params.enable = enable

            self.send_command_msg(MT_EMC_TASK_PLAN_SET_BLOCK_DELETE)

    def set_digital_output(self, index, enable):
        if not self.connected:
            return

        with self.tx_lock:
            params = self.tx.emc_command_params
            params.index = index
            params.enable = enable

            self.send_command_msg(MT_EMC_MOTION_SET_DOUT)

    def set_feed_hold_enabled(self, enable):
        if not self.connected:
            return

        with self.tx_lock:
            params = self.tx.emc_command_params
            params.enable = enable

            self.send_command_msg(MT_EMC_TRAJ_SET_FH_ENABLE)

    def set_feed_override_enabled(self, enable):
        if not self.connected:
            return

        with self.tx_lock:
            params = self.tx.emc_command_params
            params.enable = enable

            self.send_command_msg(MT_EMC_TRAJ_SET_FO_ENABLE)

    def set_axis_max_position_limit(self, axis, value):
        if not self.connected:
            return

        with self.tx_lock:
            params = self.tx.emc_command_params
            params.index = axis
            params.value = value

            self.send_command_msg(MT_EMC_AXIS_SET_MAX_POSITION_LIMIT)

    def set_axis_min_position_limit(self, axis, value):
        if not self.connected:
            return

        with self.tx_lock:
            params = self.tx.emc_command_params
            params.index = axis
            params.value = value

            self.send_command_msg(MT_EMC_AXIS_SET_MIN_POSITION_LIMIT)

    def set_optional_stop_enabled(self, enable):
        if not self.connected:
            return

        with self.tx_lock:
            params = self.tx.emc_command_params
            params.enable = enable

            self.send_command_msg(MT_EMC_TASK_PLAN_SET_OPTIONAL_STOP)

    def set_spindle_override_enabled(self, enable):
        if not self.connected:
            return

        with self.tx_lock:
            params = self.tx.emc_command_params
            params.enable = enable

            self.send_command_msg(MT_EMC_TRAJ_SET_SO_ENABLE)

    def set_spindle(self, mode, velocity=0.0):
        if not self.connected:
            return

        with self.tx_lock:
            mode_type = None
            params = self.tx.emc_command_params
            if mode == self.SPINDLE_FORWARD:
                mode_type = MT_EMC_SPINDLE_ON
                params.velocity = velocity
            elif mode == self.SPINDLE_REVERSE:
                mode_type = MT_EMC_SPINDLE_ON
                params.velocity = velocity * -1.0
            elif mode == self.SPINDLE_OFF:
                mode_type = MT_EMC_SPINDLE_OFF
            elif mode == self.SPINDLE_INCREASE:
                mode_type = MT_EMC_SPINDLE_INCREASE
            elif mode == self.SPINDLE_DECREASE:
                mode_type = MT_EMC_SPINDLE_DECRESE
            elif mode == self.SPINDLE_CONSTANT:
                mode_type = MT_EMC_SPINDLE_CONSTANT
            else:
                self.tx.Clear()
                return

            self.send_command_msg(mode_type)

    def set_spindle_override(self, scale):
        if not self.connected:
            return

        with self.tx_lock:
            params = self.tx.emc_command_params
            params.scale = scale

            self.send_command_msg(MT_EMC_TRAJ_SET_SPINDLE_SCALE)

    def set_teleop_enabled(self, enable):
        if not self.connected:
            return

        with self.tx_lock:
            params = self.tx.emc_command_params
            params.enable = enable

            self.send_command_msg(MT_EMC_TRAJ_SET_TELEOP_ENABLE)

    def set_teleop_vector(self, a, b, c, u, v, w):
        if not self.connected:
            return

        with self.tx_lock:
            params = self.tx.emc_command_params
            pose = params.pose
            pose.a = a
            pose.b = b
            pose.c = c
            pose.u = u
            pose.v = v
            pose.w = w

            self.send_command_msg(MT_EMC_TRAJ_SET_TELEOP_VECTOR)

    def set_tool_offset(self, index, zoffset, xoffset, diameter, frontangle, backangle, orientation):
        if not self.connected:
            return

        with self.tx_lock:
            params = self.tx.emc_command_params
            tooldata = params.tool_data
            tooldata.index = index
            tooldata.zoffset = zoffset
            tooldata.xoffset = xoffset
            tooldata.diameter = diameter
            tooldata.frontangle = frontangle
            tooldata.backangle = backangle
            tooldata.orientation = orientation

            self.send_command_msg(MT_EMC_TOOL_SET_OFFSET)

    def set_trajectory_mode(self, mode):
        if not self.connected:
            return

        with self.tx_lock:
            params = self.tx.emc_command_params
            params.traj_mode = mode

            self.send_command_msg(MT_EMC_TRAJ_SET_MODE)

    def unhome_axis(self, index):
        if not self.connected:
            return

        with self.tx_lock:
            params = self.tx.emc_command_params
            params.index = index

            self.send_command_msg(MT_EMC_AXIS_UNHOME)

    def shutdown(self):
        if not self.connected:
            return

        with self.tx_lock:
            self.send_command_msg(MT_SHUTDOWN)


class ApplicationError():

    NML_ERROR = MT_EMC_NML_ERROR
    NML_TEXT = MT_EMC_NML_TEXT
    NML_DISPLAY = MT_EMC_NML_DISPLAY
    OPERATOR_ERROR = MT_EMC_OPERATOR_ERROR
    OPERATOR_TEXT = MT_EMC_OPERATOR_TEXT
    OPERATOR_DISPLAY = MT_EMC_OPERATOR_DISPLAY

    def __init__(self, debug=False):
        self.threads = []
        self.shutdown = threading.Event()
        self.message_lock = threading.Lock()
        self.debug = debug
        self.is_ready = False

        self.connected = False
        self.state = 'Disconnected'
        self.socket_state = 'Down'
        self.channels = set(['error', 'text', 'display'])
        self.error_list = []

        self.error_uri = ''
        self.heartbeat_period = 0
        self.heartbeat_timer = None
        self.subscriptions = set()

        # more efficient to reuse protobuf message
        self.rx = Container()

        # ZeroMQ
        context = zmq.Context()
        context.linger = 0
        self.context = context
        self.socket = self.context.socket(zmq.SUB)
        self.sockets_connected = False

    def socket_worker(self):
        poll = zmq.Poller()
        poll.register(self.socket, zmq.POLLIN)

        while not self.shutdown.is_set():
            s = dict(poll.poll(200))
            if self.socket in s:
                self.process_error()

    def process_error(self):
        (topic, msg) = self.socket.recv_multipart()
        self.rx.ParseFromString(msg)

        if self.debug:
            print('[error] received message: %s' % topic)
            print(self.rx)

        if self.rx.type == MT_EMC_NML_ERROR \
           or self.rx.type == MT_EMC_NML_TEXT \
           or self.rx.type == MT_EMC_NML_DISPLAY \
           or self.rx.type == MT_EMC_OPERATOR_TEXT \
           or self.rx.type == MT_EMC_OPERATOR_ERROR \
           or self.rx.type == MT_EMC_OPERATOR_DISPLAY:

            error = {'type': self.rx.type, 'notes': []}
            with self.message_lock:
                for note in self.rx.note:
                    error['notes'].append(note)
                    self.error_list.append(error)
            self.refresh_error_heartbeat()

        elif self.rx.type == MT_PING:
            if self.socket_state == 'Up':
                self.refresh_error_heartbeat()
            else:
                if self.state == 'Timeout':  # waiting for the ping
                    self.update_state('Connecting')
                    self.unsubscribe()  # clean up previous subscription
                    self.subscribe()  # trigger a fresh subscribe -> full update
                else:  # ping as result from subscription received
                    self.socket_state = 'Up'
                    self.update_state('Connected')

            if self.rx.HasField('pparams'):
                interval = self.rx.pparams.keepalive_timer
                self.start_error_heartbeat(interval * 2)  # wait double the hearbeat intverval
        else:
            print('[status] received unrecognized message type')

    # returns all received messages and clears the buffer
    def get_messages(self):
        with self.message_lock:
            messages = list(self.error_list)  # make sure to return a copy
            self.error_list = []
            return messages

    def heartbeat_timer_tick(self):
        self.socket_state = 'Down'
        self.update_state('Timeout')

    def start_error_heartbeat(self, interval):
        if self.heartbeat_timer:
            self.heartbeat_timer.cancel()

        self.heartbeat_period = interval
        if interval > 0:
            self.heartbeat_timer = threading.Timer(interval / 1000,
                                               self.heartbeat_timer_tick)
            self.heartbeat_timer.start()

    def refresh_error_heartbeat(self):
        if self.heartbeat_timer:
            self.heartbeat_timer.cancel()
            self.heartbeat_timer = threading.Timer(self.heartbeat_period / 1000,
                                                   self.heartbeat_timer_tick)
            self.heartbeat_timer.start()

    def stop_error_heartbeat(self):
        if self.heartbeat_timer:
            self.heartbeat_timer.cancel()
            self.heartbeat_timer = None

    def update_state(self, state):
        if state != self.state:
            self.state = state
            if state == 'Connected':
                self.connected = True
                print('[error] connected')
            elif self.connected:
                self.connected = False
                self.stop_error_heartbeat()
                print('[error] disconnected')

    def subscribe(self):
        self.socket_state = 'Trying'

        for channel in self.channels:
            self.socket.setsockopt(zmq.SUBSCRIBE, channel)
            self.subscriptions.add(channel)

    def unsubscribe(self):
        self.socket_state = 'Down'

        for subscription in self.subscriptions:
            self.socket.setsockopt(zmq.UNSUBSCRIBE, subscription)

        self.subscriptions.clear()

    def start(self):
        self.socket_state = 'Trying'
        self.update_state('Connecting')

        if self.connect_sockets():
            self.shutdown.clear()  # in case we already used the component
            self.threads.append(threading.Thread(target=self.socket_worker))
            for thread in self.threads:
                thread.start()
            self.subscribe()

    def stop(self):
        self.is_ready = False
        self.shutdown.set()
        for thread in self.threads:
            thread.join()
        self.threads = []
        self.cleanup()
        self.update_state('Disconnected')

    def cleanup(self):
        if self.connected:
            self.unsubscribe()
        self.disconnect_sockets()
        self.subscriptions.clear()

    def connect_sockets(self):
        self.sockets_connected = True
        self.socket.connect(self.error_uri)

        return True

    def disconnect_sockets(self):
        if self.sockets_connected:
            self.socket.disconnect(self.error_uri)
            self.sockets_connected = False

    def ready(self):
        if not self.is_ready:
            self.is_ready = True
            self.start()


class ApplicationFile():

    def __init__(self, debug=True):
        self.threads = []
        self.debug = debug
        self.is_ready = False

        self.uri = ''
        self.local_file_path = ''
        self.remote_file_path = ''
        self.local_path = ''
        self.remote_path = ''
        self.transfer_state = 'NoTransfer'
        self.progress = 0.0

