# coding=utf-8
import sublime
import sublime_plugin

import os
import fnmatch
import time
import re
import json
import pickle
import subprocess
import sys
import threading
import queue
import tarfile


class RemoteEditCommand(sublime_plugin.TextCommand):

    servers = {}
    binPath = None
    settings = None
    settingFile = "RemoteEdit.sublime-settings"
    catalogFile = False
    catalog = None
    lastDir = None

    def run(self, action=None):
        # list servers
        self.items = self.loadServerList()
        self.items.insert(0, "Add new server")
        self.show_quick_panel(self.items, self.handleServerSelect)

        # Parse them for server names/info and store in self.servers[name]
        # where name is the file name less the ".server"

        # BROWSING
        # path
        # folder actions
        # up
        # list
        #
        # FOLDER ACTIONS
        # back
        # new file
        # new folder
        # rename
        # chmod
        # chown
        # delete
        #
        # FILE ACTIONS
        # edit
        # info
        # rename
        # chmod
        # chown
        # delete
        #
        # recursive ls after connect? If index is set in settings then this will
        # happen each time. It can be triggered manually or will occur after X
        # hours / days etc. ls -laR | grep -v .svn t
        #
        #
        # THOUGHTS
        # order by
        # filter
        # options for file size
        # options for date modified (possibly with monospace font?)
        #

    def handleServerSelect(self, selection):
        if selection is -1:
            return

        if selection is 0:
            # User has requested to add a new server
            sublime.error_message("TODO")
        else:
            self.startServer(self.items[selection])

    def startServer(self, serverName):
        try:
            self.serverName = serverName
            self.server = self.servers[self.serverName]
        except:
            sublime.error_message(
                "ERROR! Server \"%s\" not found." % serverName
            )
        if False:
            # Hide sys not used msg
            print(sys.version)

        # K, fire up a thread to pull down an ls and process it
        # meanwhile open a connection to the server and present the user with a
        # filelist etc
        self.catalogServer()

        # list files
        self.openServer()
        # remember state for next open
        # same path as currently open file too
        # in background grab path and save as dict???

    def openServer(self):
        if not self.lastDir:
            self.lastDir = self.getServerSetting(
                "remote_path",
                "/home/%s" % self.getServerSetting("user")
            )
        print("BOUT TO LIST")
        s = self.listDirectory(self.lastDir)
        if not s:
            # error message
            return sublime.error_message(
                "Error connecting to %s" % self.serverName
            )
        # Show the options
        self.show_quick_panel(self.items, self.handleList)

    def handleList(self, selection):
        print(selection, self.items[selection])
        if selection == -1:
            try:
                self.pq["p"].terminate()
            except:
                pass
            return
        elif selection == 0:
            # text of server / dir
            caption = "Navigate to: "
            self.show_input_panel(
                caption,
                "%s" % self.lastDir,
                self.handleNavigate,
                self.handleChange,
                self.handleCancel
            )
        elif selection == 1:
            # Folder options
            self.folderOptions = [
                " : Back to list",
                " : New file",
                " : New folder",
                " : Rename",
                " : Chmod",
                " : Delete"
            ]
            self.show_quick_panel(self.folderOptions, self.handleFolderOptions)
        elif selection == 2 or self.items[selection][-1] == "/":
            # Up a folder
            if selection == 2:
                if len(self.lastDir) <= 1:
                    self.lastDir = "/"
                else:
                    if self.lastDir[-1] == "/":
                        self.lastDir = self.lastDir[0:-1]
                    self.lastDir = self.lastDir[0:self.lastDir.rfind("/") + 1]
            else:
                if not self.lastDir[-1] == "/":
                    self.lastDir += "/"
                self.lastDir += self.items[selection]
            s = self.listDirectory(self.lastDir)
            if not s:
                # error message
                return sublime.error_message(
                    "Error connecting to %s" % self.serverName
                )
            # Show the options
            self.show_quick_panel(self.items, self.handleList)
        else:
            if not self.downloadAndOpen(self.items[selection]):
                return sublime.error_message(
                    "Error connecting to %s" % self.serverName
                )

    def handleNavigate(self, path):
        prevDir = self.lastDir
        self.lastDir = path
        s = self.listDirectory(path)
        if not s:
            self.lastDir = prevDir
            # error message
            sublime.error_message(
                "Error navigating to %s" % path
            )
        # Show the options
        self.show_quick_panel(self.items, self.handleList)

    def handleFolderOptions(self, selection):
        if selection == -1:
            try:
                self.pq["p"].terminate()
            except:
                pass
            return
        elif selection == 0:
            # Back to prev list
            self.show_quick_panel(self.items, self.handleList)
        elif selection == 1:
            # new file
            caption = "Enter file name: "
            self.show_input_panel(
                caption,
                "",
                self.handleNewFile,
                self.handleChange,
                self.handleCancel
            )
        elif selection == 2:
            # new folder
            caption = "Enter folder name: "
            self.show_input_panel(
                caption,
                "",
                self.handleNewFolder,
                self.handleChange,
                self.handleCancel
            )
        elif selection == 3:
            # rename
            pass
        elif selection == 4:
            # chmod
            pass
        elif selection == 5:
            # delete
            pass
        else:
            # we shouldn't ever get here
            return

    def handleNewFile(self, fileName):
        if not fileName:
            self.show_quick_panel(self.folderOptions, self.handleFolderOptions)
        else:
            # make local folder
            localFolder = self.makeLocalFolder()
            if not localFolder:
                # error message
                return sublime.error_message(
                    "Error creating local folder"
                )
            # Make local file, set sftp flags
            # open in editor
            pass

    def handleNewFolder(self, folderName):
        if not folderName:
            self.show_quick_panel(self.folderOptions, self.handleFolderOptions)
        else:
            if not self.connectionOpen():
                # error message
                return sublime.error_message(
                    "Error connecting to %s" % self.serverName
                )
            # Create folder on server
            if not self.sendCommand("mkdir %s" % folderName):
                print("MKDIR FAIL")
                return False
            (out, err) = self.readUntilReady(self.pq)
            if "psftp>" not in out:
                print("MKDIR RET FAIL")
                return False
            # remote cd to folder
            self.lastDir = "%s%s/" % (self.lastDir, folderName)
            if not self.sendCommand("cd %s" % self.lastDir):
                print("CD FAIL")
                return False
            (out, err) = self.readUntilReady(self.pq)
            if "psftp>" not in out:
                print("CD RET FAIL")
                return False
        self.items = []
        self.addOptionsToItems()
        self.show_quick_panel(self.items, self.handleList)

    def addOptionsToItems(self):
        self.items.insert(0, " : Up a folder..")
        self.items.insert(0, " : Folder actions")
        self.items.insert(0, "%s:%s" % (
            self.getServerSetting("host"),
            self.lastDir
        ))

    def makeLocalFolder(self):
        # file selected, ensure local folder is available
        localFolder = os.path.join(
            os.path.expandvars("%temp%"),
            "RemoteEdit",
            self.serverName
        )
        for f in self.lastDir.split("/"):
            if f:
                localFolder = os.path.join(
                    localFolder,
                    f
                )
        if not localFolder[-1] == "/":
            localFolder += "/"
        try:
            os.makedirs(localFolder)
        except Exception as e:
            # TODO: If exception is not "folders present" then return false
            print("EXCEP WHEN MAKING LOCAL FOLDER: %s" % e)
        return localFolder

    def downloadAndOpen(self, f):
        localFolder = self.makeLocalFolder()
        if not localFolder:
            # error message
            return sublime.error_message(
                "Error creating local folder"
            )
        if not self.connectionOpen():
            # error message
            return sublime.error_message(
                "Error connecting to %s" % self.serverName
            )
        # cd
        if self.pq["pwd"] != self.lastDir:
            if not self.sendCommand("cd %s" % self.lastDir):
                print("CD FAIL")
                return False
            (out, err) = self.readUntilReady(self.pq)
            if "psftp>" not in out:
                print("CD RET FAIL")
                return False
        if not self.sendCommand("get %s %s%s" % (f, localFolder, f)):
            print("GET FAIL")
            return False
        (out, err) = self.readUntilReady(self.pq)
        if "psftp>" not in out:
            print("GET RET FAIL")
            return False
        print("OUT: %s" % out)
        self.view.window().open_file("%s%s" % (localFolder, f))
        return True

    def connectionOpen(self):
        try:
            print("POLLING")
            if self.pq["p"].poll() is None:
                print("POLLING OK")
                return True
            else:
                print("POLLING FAIL BUT p PRESENT")
        except:
            print("EXCEPT TRIGGERED")
        # need to reconnect
        cmd = self.getCommand("psftp.exe")
        self.pq = self.getProcess(cmd)
        print("OPENING")
        (out, err) = self.readUntilReady(self.pq)
        print("OPEN: %s" % out)
        if "psftp>" not in out:
            return False
        return True

    def sendCommand(self, cmd):
        try:
            print("CMD: %s" % cmd)
            self.pq["p"].stdin.write(bytes("%s\n" % cmd, "utf-8"))
            return True
        except Exception as e:
            print("EXC: %s" % e)
            return False

    def listDirectory(self, d):
        if self.catalog:
            # Display options based on the catalog and self.lastDir
            try:
                self.items = self.catalog[d][:]
                self.addOptionsToItems()
                return True
            except:
                print("%s NOT IN CATALOG" % d)
        if not self.connectionOpen():
            # error message
            return sublime.error_message(
                "Error connecting to %s" % self.serverName
            )
        if not self.sendCommand("cd %s" % d):
            print("CD FAIL")
            return False
        print("CD SENT")
        (out, err) = self.readUntilReady(self.pq)
        if "no such file or directory" in out:
            return False
        print("AAA", out, "BBB", err, "CCC")
        if "psftp>" not in out:
            return False
        self.pq["pwd"] = d
        if not self.sendCommand("ls"):
            print("LS FAIL")
            return False
        print("LS SENT")
        (out, err) = self.readUntilReady(self.pq)
        print("DDD", out, "EEE", err, "FFF")
        # parse out
        items = []
        for line in out.split("\n"):
            la = line.split(" ")
            f = la[-1].strip()
            if f:
                if la[0][0] == "d":
                    items.append(f + "/")
                else:
                    items.append(f)
        if len(items) >= 3:
            self.items = items[3:]
        self.addOptionsToItems()
        return True

    def catalogServer(self):
        if not self.getServerSetting("cache_file_structure"):
            return
        if not self.getServerSetting("remote_path"):
            return

        # First, see if we've already got a catalog and it's
        # recent (less than 1 day old)
        self.catalogFile = os.path.join(
            sublime.packages_path(),
            "User",
            "RemoteEdit",
            "%s.catalog" % self.serverName
        )
        try:
            mTime = os.path.getmtime(self.catalogFile)
            stale = self.getSettings().get("catalog_stale_after_hours", 24) * 3600
            self.catalog = None
        except:
            mTime = stale = 0
        if mTime + stale < time.time():
            # needs a refresh
            # todo, move this to a background thread
            # todo, set a flag for known hosts after first connect
            # if the flag is set we can punt the plink query straight into the
            # background thread without worrying about known hosts

            cmd = self.getCommand("plink.exe")
            pq = self.getProcess(cmd)
            (out, err) = self.readUntilReady(pq)
            print("AAA", out, "BBB", err, "CCC")

            if "host key is not cached in the registry" in err:
                send = "n\n"
                pq["p"].stdin.write(bytes(send, "utf-8"))
                (out, err) = self.readUntilReady(pq)
                print("DDD", out, "EEE", err, "FFF")

            # We should be at a prompt
            send = "cd %s && ls -lahpR --time-style=long-iso > /tmp/%sSub.cat || cd /tmp && tar cfz %sSub.tar.gz %sSub.cat && rm %sSub.cat && echo $((666 + 445));\n" % (
                self.getServerSetting("remote_path"),
                self.serverName,
                self.serverName,
                self.serverName,
                self.serverName
            )
            pq["p"].stdin.write(bytes(send, "utf-8"))
            (out, err) = self.readUntilReady(pq)
            print("GGG", out, "HHH", err, "III")
            if not "1111" in out:
                # Try 1 more time as it pauses when running the command
                # so we stop capturing
                for i in range(10):
                    time.sleep(1)
                    (out, err) = self.readUntilReady(pq)
                    if not "1111" in out:
                        print("Not found! %s" % out)
                    else:
                        print("JJJ", out, "KKK", err, "LLL")
                        break
            try:
                pq["p"].terminate()
            except:
                pass

            # Now grab the file
            cmd = self.getCommand("psftp.exe")
            pq = self.getProcess(cmd)
            (out, err) = self.readUntilReady(pq)
            print("AAA", out, "BBB", err, "CCC")
            if "psftp>" not in out:
                return False
            try:
                pq["p"].stdin.write(bytes("cd /tmp\n", "utf-8"))
            except Exception as e:
                print("EXC: %s" % e)
                return False
            (out, err) = self.readUntilReady(pq)
            print("AAA", out, "BBB", err, "CCC")
            if "psftp>" not in out:
                return False
            localFolder = os.path.join(
                os.path.expandvars("%temp%"),
                "RemoteEdit",
                self.serverName
            )
            if not localFolder[-1] == "/":
                localFolder += "/"
            try:
                os.makedirs(localFolder)
            except Exception as e:
                # TODO: If exception is not "folders present" then return false
                print("EXCEP WHEN MAKING LOCAL FOLDER: %s" % e)
            fileName = "%sSub.tar.gz" % self.serverName
            localFile = os.path.join(
                localFolder,
                fileName
            )
            try:
                pq["p"].stdin.write(bytes("get %s %s\n" % (
                    fileName,
                    localFile
                ), "utf-8"))
            except Exception as e:
                print("EXC: %s" % e)
                return False
            (out, err) = self.readUntilReady(pq)
            print("AAA", out, "BBB", err, "CCC")
            if "psftp>" not in out:
                print("GET RET FAIL")
                return False
            # delete tmp file from server
            try:
                pq["p"].stdin.write(bytes("del %s\n" % (
                    fileName
                ), "utf-8"))
            except Exception as e:
                print("EXC DEL: %s" % e)
                return False
            (out, err) = self.readUntilReady(pq)
            if "psftp>" not in out:
                print("DEL RET FAIL")
                return False
            # check local file exists
            try:
                pq["p"].terminate()
            except:
                pass
            try:
                f = tarfile.open(localFile, "r:gz")
                f.extractall(localFolder)
                f.close()
            except:
                print("GZIP EXC")
                return False
            f = open(os.path.join(
                localFolder,
                "%sSub.cat" % self.serverName
            ), "r", encoding="utf-8")
            struc = {}
            startAt = self.getServerSetting("remote_path")
            if startAt[-1] != "/":
                startAt += "/"
            for line in f:
                line = line.strip()
                if len(line) and line[-1] == ':':
                    key = "%s%s" % (startAt, line[2:-1])
                    if key[-1] != "/":
                        key += "/"
                    options = []
                elif not line:
                    struc[key] = options
                else:
                    sl = line.split(" ")
                    name = sl[-1]
                    if len(sl) > 2 and name != "./" and name != "../":
                        # name = "%s%s" % (
                        #     name,
                        #     "/" if sl[0][0] == "d" else ""
                        # )
                        options.append(name)
            f = open(self.catalogFile, "wb")
            pickle.dump(struc, f)
            f.close()
            print("CATALOG'D")
        self.catalog = pickle.load(open(
            self.catalogFile,
            "rb"
        ))

    def getCommand(self, app):
        cmd = [
            os.path.join(self.getBinPath(), app),
            "-agent",
            self.getServerSetting("host"),
            "-l",
            self.getServerSetting("user")
        ]
        if "psftp" not in app:
            cmd.append("-ssh")
        if self.getServerSetting("port", None):
            cmd.append("-P")
            cmd.append(self.getServerSetting("port"))
        if self.getServerSetting("ssh_key_file", None):
            cmd.append("-i")
            cmd.append(self.getServerSetting("ssh_key_file"))
        return cmd

    def getProcess(self, cmd):
        kwargs = {}
        if subprocess.mswindows:
            su = subprocess.STARTUPINFO()
            su.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            su.wShowWindow = subprocess.SW_HIDE
            kwargs['startupinfo'] = su

        pq = {}
        pq["p"] = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            close_fds=False,
            **kwargs
        )
        pq["qo"] = queue.Queue()
        pq["qe"] = queue.Queue()
        pq["pwd"] = None
        to = threading.Thread(target=enqueue_output, args=(pq["p"].stdout, pq["qo"]))
        te = threading.Thread(target=enqueue_output, args=(pq["p"].stderr, pq["qe"]))
        to.daemon = True
        te.daemon = True
        to.start()
        te.start()
        return pq

    def readPipes(self, pq):
        out = err = ""
        # read line without blocking
        try:
            err = pq["qe"].get_nowait()
        except queue.Empty:
            pass
        # read line without blocking
        try:
            out = pq["qo"].get_nowait()
        except queue.Empty:
            pass
        return (out, err)

    def readUntilReady(self, pq):
        print("READ UNTIL READY CALLED")
        out = err = ""
        i = 0
        while True:
            (outB, errB) = self.readPipes(pq)
            out += str(outB)
            err += str(errB)
            if pq["p"].poll() is not None:
                break
            elif (len(out) or len(err)) and not outB and not errB:
                i += 1
                if i > 5:
                    break
            time.sleep(0.01)
        return (out, err)

    def getBinPath(self):
        if not self.binPath:
            self.binPath = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "bin"
            )
            # ST2 on XP managed to get the path wrong with the above line
            if not os.path.exists(
                os.path.join(self.binPath, "psftp.exe")
            ):
                self.binPath = os.path.join(
                    sublime.packages_path(),
                    "RemoteEdit",
                    "bin"
                )
        return self.binPath

    def getServerSetting(self, key, default=None):
        try:
            val = self.server["settings"][key]
        except:
            val = default
        return val

    def removeComments(self, text):
        """Thanks to: http://stackoverflow.com/questions/241327/"""
        def replacer(match):
            s = match.group(0)
            if s.startswith('/'):
                return ""
            else:
                return s
        pattern = re.compile(
            r'//.*?$|/\*.*?\*/|\'(?:\\.|[^\\\'])*\'|"(?:\\.|[^\\"])*"',
            re.DOTALL | re.MULTILINE
        )
        return re.sub(pattern, replacer, text)

    def jsonify(self, data):
        """Return a dict from the passed string of json"""
        self.lastJsonifyError = None
        try:
            # Remove any comments from the files as they're not technically
            # valid JSON and the parser falls over on them
            data = self.removeComments(data)

            return json.loads(data, strict=False)
        except Exception as e:
            self.lastJsonifyError = "Error parsing JSON: %s" % str(e)
            print(self.lastJsonifyError)
            return False

    def loadServerList(self):
        # Load all files in User/RemoteEdit ending in ".server"
        serverList = []
        serverConfigPath = os.path.join(
            sublime.packages_path(),
            "User",
            "RemoteEdit"
        )
        for root, dirs, files in os.walk(serverConfigPath):
            for filename in fnmatch.filter(files, "*.server"):
                serverName = filename[0:-7]
                serverList.append(serverName)
                self.servers[serverName] = {}
                self.servers[serverName]["path"] = os.path.join(root, filename)
                self.servers[serverName]["settings"] = self.jsonify(
                    open(self.servers[serverName]["path"]).read()
                )
        return serverList

    def getSettings(self):
        if not self.settings:
            self.settings = sublime.load_settings(self.settingFile)
        return self.settings

    def saveSettings(self):
        sublime.save_settings(self.getSettings())

    def show_quick_panel(self, options, done):
        sublime.set_timeout(
            lambda: self.view.window().show_quick_panel(options, done),
            10
        )

    def show_input_panel(self, caption, initialtext, done, change, cancel):
        sublime.set_timeout(
            lambda: self.view.window().show_input_panel(
                caption,
                initialtext,
                done,
                change,
                cancel
            ),
            10
        )

    def handleChange(self, selection):
        return

    def handleCancel(self):
        return


def enqueue_output(out, queue):
    # for line in iter(out.readline, b''):
    while True:
        line = out.read(1000)
        queue.put(str(line, "utf-8"))
        if not len(line):
            break
    # out.close()
