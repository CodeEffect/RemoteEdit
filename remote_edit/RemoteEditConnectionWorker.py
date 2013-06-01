# coding=utf-8
import subprocess
import threading
import queue
import os
import time


# Command dict:
#   work["server_name"] = string server name
#   work["settings"] = server settings dict
#   work["cmd"] = command string
#   work["prompt_contains"] = the string to look for in the response that signals
#       we have everything we need back and can stop listening when the data stops.
#   work["listen_attempts"] = how many times to listen for data until we get back
#       our specified promptContains
#   work["key"] = uniquely identifying key used to return the result data
#
# Return dict:
#   data["key"] = uniquely identifying key used to return the result data
#   data["out"] = what stdout spewed
#   data["err"] = ditto stderr
#   data["success"] = bool indicating if the expected response was returned
#   data["failure_reason_id"] = TODO: an error reason indicator (timed out,
#       permission denied etc)


class RemoteEditConnectionWorker(threading.Thread):

    threadId = None
    process = None
    queueOut = None
    queueErr = None
    threadOut = None
    threadErr = None
    lastErr = None
    lastOut = None
    binPath = None
    promptContains = None
    quit = False

    serverName = None
    appType = None
    queue = None
    results = None
    work = None

    def config(self, threadId, appType, queue, results):
        self.threadId = threadId
        self.appType = appType
        self.queue = queue
        self.results = results
        if self.appType == "sftp":
            self.promptContains = "psftp>"
        else:
            self.promptContains = "$"
        self.debug("INIT1")

    def __del__(self):
        self.quit = True
        self.close_connection()

    def run(self):
        holdYerHorses = 0.01
        while not self.quit:
            # Can't do anything until we're config'd (above). Tried using
            # __init__ but the rest of the code had trouble seeing var's
            # declared there. Bug?
            if self.queue:
                self.debug("Start work loop")
                self.work = self.queue.get()
                self.process_work_and_respond()
                self.queue.task_done()
                self.debug("End work loop")
            else:
                time.sleep(holdYerHorses)

    def process_work_and_respond(self):
        # Check to see if we've been told to terminate
        if "KILL" in self.work:
            self.stop(self.work["KILL"])
            return
        # If we're connected to a different server then disconnect
        if self.serverName and self.work["server_name"] != self.serverName:
            self.debug("Server has changed. Before: %s, After: %s" % (self.serverName, self.work["server_name"]))
            self.close_connection()
        # Run the command
        success = self.run_command(
            self.work["cmd"],
            self.work["prompt_contains"],
            self.work["listen_attempts"]
        )
        # Put together the results object and add it to the dict shated with
        # the parent
        results = {}
        results["success"] = success
        results["out"] = self.lastOut
        results["err"] = self.lastErr
        # results["failure_reason_id"]
        self.results[self.work["key"]] = results

    def stop(self, threadId):
        self.debug("STOP called for %s, we are %s" % (threadId, self.threadId))
        if self.threadId == threadId:
            self.quit = True
            self.close_connection()
            self.debug("Thread %s has left the building." % threadId)

    def run_command(self, cmd, checkReturn=None, listenAttempts=1):
        # Record which server we're connected to
        self.serverName = self.work["server_name"]
        # If checkReturn is overridden on a per command basis then it only
        # applies to the self.write_command() call. We will still need to look
        # for the standard prompt text after we connect to the server.
        promptContains = self.get_server_setting(
            "prompt_contains",
            self.promptContains
        )
        if checkReturn is None:
            checkReturn = promptContains
        if not self.connect(promptContains):
            self.debug("Error connecting")
            return False
        # Write the cmd string to stdin
        if cmd and not self.write_command(cmd):
            self.debug("Error writing")
            return False
        buf = ""
        # If not found then try again.
        while listenAttempts > 0:
            self.debug("Now listening")
            self.await_response()
            buf += self.lastOut
            if checkReturn in self.lastOut:
                self.debug("Found checkReturn")
                break
            listenAttempts -= 1
        self.lastOut = buf
        if checkReturn not in self.lastOut:
            self.debug("Expected return data not found")
            return False
        return True

    def connect(self, promptContains):
        try:
            if self.process.poll() is None:
                self.debug(":o) Polling ok, process alive and well")
                return True
            else:
                self.debug("Polling fail, process has died")
        except Exception as e:
            self.debug("Process not running: %s" % e)
        # Need to reconnect
        self.create_process()
        self.await_response()
        if promptContains not in self.lastOut:
            self.await_response()
            if promptContains not in self.lastOut:
                self.debug("Connect failed: %s" % self.lastOut)
                return False
        self.debug("Connection OK")
        return True

    def write_command(self, cmd):
        try:
            self.debug("Sending command: %s" % cmd)
            self.process.stdin.write(bytes("%s\n" % cmd, "utf-8"))
            return True
        except Exception as e:
            self.debug("Command failed: %s" % e)
            return False

    def await_response(self):
        self.debug("Waiting for output...")
        self.lastOut = self.lastErr = ""
        i = 0
        while True:
            (outB, errB) = self.read_pipes()
            self.lastOut += str(outB)
            self.lastErr += str(errB)
            # This code was to check to see if the process has died. Before we
            # moved to a subprocess / thread communication model this worked
            # fine. With the current code the process lies! After the second
            # stdin write it now reports a returncode of 1 but keeps running.
            if self.process.poll() is not None:
                self.debug("Process died")
                break
            if (self.lastOut or self.lastErr) and not outB and not errB:
                i += 1
                if i > 10:
                    break
            time.sleep(0.01)
        if self.lastOut:
            self.debug(
                "--------- OUT ---------\n%s\n%s" % (
                    "\n".join(map(self.strip, self.lastOut.split("\n"))),
                    "-" * 40
                )
            )
        if self.lastErr:
            self.debug(
                "-------- ERROR --------\n%s\n%s" % (
                    "\n".join(map(self.strip, self.lastErr.split("\n"))),
                    "-" * 40
                )
            )

    def strip(self, s):
        return s.strip()

    def read_pipes(self):
        out = err = ""
        # Read line without blocking
        try:
            err = self.queueErr.get_nowait()
        except queue.Empty:
            pass
        # Read line without blocking
        try:
            out = self.queueOut.get_nowait()
        except queue.Empty:
            pass
        return (out, err)

    def close_connection(self):
        try:
            self.process.terminate()
        except:
            pass

    def get_local_command(self):
        cmd = [
            self.get_app_path(),
            "-agent",
            self.get_server_setting("host"),
            "-l",
            self.get_server_setting("user")
        ]
        if self.appType == "ssh":
            cmd.append("-ssh")
        if self.get_server_setting("port", None):
            cmd.append("-P")
            cmd.append(self.get_server_setting("port"))
        if self.get_server_setting("password", None):
            cmd.append("-pw")
            cmd.append(self.get_server_setting("password"))
        sshKeyFile = self.get_server_setting("ssh_key_file", None)
        if sshKeyFile:
            if "%" in sshKeyFile:
                sshKeyFile = os.path.expandvars(sshKeyFile)
                cmd.append("-i")
                cmd.append(sshKeyFile)
        return cmd

    def get_app_path(self):
        if self.appType == "sftp":
            app = "psftp.exe"
        elif self.appType == "ssh":
            app = "plink.exe"
        else:
            raise Exception("Unknown app type")
        return os.path.join(
            self.get_bin_path(),
            app
        )

    def get_server_setting(self, key, default=None):
        try:
            val = self.work["settings"][key]
        except:
            val = default
        return val

    def create_process(self):
        kwargs = {}
        if subprocess.mswindows:
            su = subprocess.STARTUPINFO()
            su.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            su.wShowWindow = subprocess.SW_HIDE
            kwargs['startupinfo'] = su
        self.process = subprocess.Popen(
            self.get_local_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            close_fds=False,
            **kwargs
        )
        self.queueErr = queue.Queue()
        self.queueOut = queue.Queue()
        self.threadOut = threading.Thread(
            target=enqueue_output,
            args=(self.process.stdout, self.queueOut)
        )
        self.threadErr = threading.Thread(
            target=enqueue_output,
            args=(self.process.stderr, self.queueErr)
        )
        self.threadOut.daemon = True
        self.threadErr.daemon = True
        self.threadOut.start()
        self.threadErr.start()
        return self.process

    def get_bin_path(self):
        if not self.binPath:
            self.binPath = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "bin"
            )
        return self.binPath

    def debug(self, data):
        if len(data) > 3000:
            print("%s[%s] %s: %s" % (
                self.appType.upper(),
                self.threadId,
                time.strftime("%H:%M:%S"),
                data[0:3000]
            ))
        else:
            print("%s[%s] %s: %s" % (
                self.appType.upper(),
                self.threadId,
                time.strftime("%H:%M:%S"),
                data
            ))


def enqueue_output(out, queue):
    while True:
        line = out.read(1000)
        queue.put(str(line, "utf-8", errors="ignore"))
        if not len(line):
            break
