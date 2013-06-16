# coding=utf-8
import sublime
import sublime_plugin

import os
import fnmatch
import time
import re
import json
import pickle
import tarfile
import queue
import hashlib
from .remote_edit import RemoteEditConnectionWorker


class RemoteEditCommand(sublime_plugin.WindowCommand):

    items = None
    itemPaths = None
    selected = None
    movingFrom = None
    servers = {}
    serverName = None
    fuzzyServer = None
    settings = None
    settingFile = "RemoteEdit.sublime-settings"
    bookmarksSettingsFile = "RemoteEditBookmarks.sublime-settings"
    cat = {}
    catFile = False
    forceReloadCat = True
    lastDir = None
    prevDir = None
    browsingMode = "edit"
    fileInfo = False
    showHidden = False
    dontEditExt = []
    catExcludeFolders = []
    bgCat = 0
    bgCatStep = 0
    permsLookup = None
    lsParams = "-lap --time-style=long-iso --color=never"
    unixLsParams = "-lapD\"%Y-%m-%d %H:%M\""
    orderBy = "name"
    orderReverse = False
    tempPath = "/tmp"
    timeout = 60
    connector = None
    platform = sublime.platform()
    FILE_TYPE_FILE = 0
    FILE_TYPE_FOLDER = 1
    FILE_TYPE_SYMLINK = 2
    SORT_BY_NAME = 10
    SORT_BY_EXT = 11
    # Folders then files
    SORT_BY_TYPE = 0
    SORT_BY_SIZE = 4
    SORT_BY_MODIFIED = 5
    STAT_KEY_TYPE = 0
    STAT_KEY_PERMISSIONS = 1
    STAT_KEY_USER = 2
    STAT_KEY_GROUP = 3
    STAT_KEY_SIZE = 4
    STAT_KEY_MODIFIED = 5
    STAT_KEY_DESTINATION = 6

    # per server filename filters, add filters
    # filter by file size
    # filter by date modified
    #
    # Update progress of background recursive ls so that it can be picked up if it dies
    #
    # Copy directories to another location / server
    # server remote_path setting as dict + auto add to bookmarks.
    # settings
    # logging to file + add debug level and allow turning off
    # configure server settings menu
    # multiple cat locations
    # status bar busy
    # Server health (disks, htop etc) Status bar?
    # keepalives
    # host key not cached
    # SVN switch etc etc for configured dir's

    #  No fucking interactive shit ubuntu!!!!!
    #  no access to tty bad file description (shut the fuck up again!!!!)
    #  Tail a log file, awesome!!!!!

    def run(self, fileName=None, serverName=None, lineNumber=None, action=None, save=None):
        # Ensure that the self.servers dict is populated
        self.load_server_list()
        # Load the connector
        if not self.connector:
            self.connector = RemoteEditConnector(self.window)
        if action == "save" and save:
            # If save was called from the external RE events handler class then save
            # the file back to the server
            self.save(save)
        elif action == "on_app_start":
            # Tidy up our local temp folder. This should only contain files that are
            # open but occasionally if a command fails halfway through it doesn't
            # keep things tidy
            self.tidy_local_tmp_path()
        elif action == "fuzzy":
            # If fuzzy was passed as a command arg then we display the fuzzy file
            # list from the catalogue
            # Work out which server we're listing
            self.fuzzyServer = self.get_settings().get(
                "default_fuzzy_server",
                self.fuzzyServer
            )
            # If nothing set so far then try self.serverName
            if not self.fuzzyServer:
                self.fuzzyServer = self.serverName
            if self.fuzzyServer:
                self.server = self.servers[self.fuzzyServer]
                self.serverName = self.fuzzyServer
                self.items = []
                if not self.cat or "/CAT_DATA/" not in self.cat or "loaded" not in self.cat["/CAT_DATA/"]:
                    self.check_cat()
                    if not self.cat or "/CAT_DATA/" not in self.cat or "loaded" not in self.cat["/CAT_DATA/"]:
                        self.error_message("Error loading catalogue. This may be fixed by waiting a few minutes for a new one to be prepared of there may be a more permanent issue. The console should have more information.")
                fuzzPath = self.get_server_setting(
                    "fuzzy_path",
                    self.get_server_setting("cat_path")
                )
                self.append_files_from_path(
                    self.get_file_from_cat(fuzzPath),
                    fuzzPath
                )
                self.show_quick_panel(self.items, self.handle_fuzzy)
            else:
                self.error_message("No server selected. Please connect and select a default server")
        elif fileName and serverName and lineNumber:
            # Else, if we have a passed filename, server and line number from the
            # mouse click event handler then a search result has been clicked on.
            # Download the file and open to edit.
            self.serverName = serverName
            self.server = self.servers[self.serverName]
            self.download_and_open(fileName, lineNumber=lineNumber)
        elif self.serverName:
            # If we get this far then it's just a request to start normally. If
            # serverName is set then we can go straight to browsing that server
            self.start_server(self.serverName)
        else:
            # Lastly, no serer has yet been selected, display the server list and
            # other options
            self.items = [name for name in sorted(self.servers)]
            items = [[
                "  %s (%s)" % (name, self.servers[name]["settings"]["host"]),
                "  User: %s, Path: %s" % (
                    self.servers[name]["settings"]["user"],
                    self.servers[name]["settings"]["remote_path"]
                )
            ] for name in self.items]
            items.insert(0, [
                "» Quick connect",
                "Just enter a host and a username / password"
            ])
            items.insert(0, [
                "» Add a new server",
                "Complete new server details to quickly connect in future"
            ])
            self.show_quick_panel(items, self.handle_server_select)

    def load_server_list(self):
        if self.servers:
            return
        # Load all files in User/RemoteEdit/Servers folder
        serverConfigPath = self.get_server_config_path()
        if not os.path.exists(serverConfigPath):
            try:
                os.makedirs(serverConfigPath)
            except:
                pass
        for root, dirs, files in os.walk(serverConfigPath):
            for filename in fnmatch.filter(files, "*"):
                serverName = filename[0:filename.rfind(".")]
                self.servers[serverName] = {}
                self.servers[serverName]["path"] = os.path.join(root, filename)
                self.servers[serverName]["settings"] = self.jsonify(
                    open(self.servers[serverName]["path"]).read()
                )
        # If only 1 server found then it can be our default fuzzy find server
        if len(self.servers) == 1:
            self.fuzzyServer = serverName

    def save(self, id):
        # Do some basic sanity checks on our data
        if not id:
            return self.error_message("Save called without specifying view")
        # First get the view with the passed id so we can grab data
        for v in self.window.views():
            if v.id() == id:
                break
        else:
            return self.error_message("View %s not found" % id)
        # Check our file is within the temp editing folder
        tempFolder = self.get_local_tmp_path(False)
        localFile = v.file_name()
        if tempFolder not in localFile:
            return self.error_message(
                "View %s has incorrect path %s" % (id, localFile)
            )
        # Validate our stored RE data
        reData = v.settings().get("reData", None)
        if not reData:
            return self.error_message("View data not available for %s" % id)
        # Set a lock to ensure that we're the only thread running the save
        lockFile = localFile + ".lock"
        # Acquire lock
        if not self.acquire_lock(lockFile):
            # Could not obtain lock
            # TODO: Check the age of the folder / lock
            debug("File is being saved already")
            return
        remoteFile = self.join_path(
            reData["path"],
            reData["fileName"]
        )
        serverName = reData["serverName"]
        if self.serverName != serverName:
            try:
                self.server = self.servers[serverName]
            except:
                self.release_lock(lockFile)
                return self.error_message(
                    "Missing connection details for server \"%s\"" % serverName
                )
        cP = {}
        cP["lockFile"] = lockFile
        cP["viewId"] = id
        cP["remoteFile"] = remoteFile
        cP["serverName"] = serverName
        cP["lockFile"] = lockFile
        # Initiate the save
        cmd = "put %s %s" % (
            self.escape_remote_path(localFile),
            self.escape_remote_path(remoteFile)
        )
        self.run_sftp_command(
            cmd,
            callback=self.save_callback,
            cP=cP
        )

    def save_callback(self, results, cP):
        self.release_lock(cP["lockFile"])
        view = self.window.active_view()
        if view.id() != cP["viewId"]:
            # Not active at the mo, let's search the open views for it
            for v in self.window.views():
                if v.id() == cP["viewId"]:
                    view = v
                    break
        if view.id() != cP["viewId"]:
            # The view has gone so no point doing anything that touches it
            view = None
        if not results["success"]:
            # Mark view as dirty
            if view:
                view.run_command("remote_edit_mark_dirty", {'id': cP["viewId"]})
            return self.error_message("Error saving remote file %s to %s" % (
                cP["remoteFile"],
                cP["serverName"]
            ))
        # if succeeded then display ok
        if "permission denied" in results["out"]:
            # Mark view as dirty
            if view:
                view.run_command("remote_edit_mark_dirty", {'id': cP["viewId"]})
            return self.error_message(
                "Permission denied when attempting to write file %s to %s" % (
                    cP["remoteFile"],
                    cP["serverName"]
                )
            )
        (path, fileName) = self.split_path(cP["remoteFile"])
        if view:
            reData = view.settings().get("reData", None)
            reData["remote_save"] = time.time()
            view.settings().set("reData", reData)

    def acquire_lock(self, path):
        try:
            os.mkdir(path)
            return True
        except:
            return False

    def release_lock(self, path):
        try:
            os.rmdir(path)
        except:
            pass
        return True

    def handle_server_select(self, selection):
        if selection is -1:
            return
        elif selection is 0:
            # User has requested to add a new server
            # Open a new tab and populate it with the defult new server snippet
            saveTo = self.get_server_config_path()
            snippet = sublime.load_resource(
                "Packages/RemoteEdit/NewServer.default-config"
            )
            if self.platform == "windows":
                keyLocation = "${9:%UserProfile%\\Documents\\putty.ppk}"
            else:
                keyLocation = "~/.ssh/id_rsa"
            snippet = snippet.replace("%%KEY_LOCATION%%", keyLocation)
            newSrv = self.window.new_file()
            newSrv.set_name("NewServer.sublime-settings")
            newSrv.set_syntax_file("Packages/JavaScript/JSON.tmLanguage")
            newSrv.settings().set("default_dir", saveTo)
            self.insert_snippet(snippet)
        elif selection is 1:
            # User has requested to quick connect
            self.show_input_panel(
                "Enter connection string (user@hostname:port/remote/path): ",
                "",
                self.handle_quick_host,
                self.handle_change,
                self.handle_cancel
            )
        else:
            # A server has been selected from the list
            self.start_server(self.items[selection - 2])

    def insert_snippet(self, snippet):
        view = self.window.active_view()
        if view.is_loading():
            sublime.set_timeout(lambda: self.insert_snippet(snippet), 100)
        else:
            view.run_command("insert_snippet", {"contents": snippet})

    def start_server(self, serverName, quickConnect=False):
        try:
            self.prevDir = None
            if self.serverName != serverName:
                self.bgCat = 0
                self.serverName = serverName
                self.server = self.servers[serverName]
                self.forceReloadCat = True
                self.lastDir = self.get_server_setting("remote_path", None)
                self.orderBy = self.parse_order_by_setting(
                    self.get_server_setting("order_by", self.orderBy)
                )
                self.orderReverse = self.get_server_setting(
                    "order_reverse",
                    self.orderReverse
                )
                self.showHidden = self.get_server_setting("show_hidden", False)
                self.browsingMode = self.get_server_setting(
                    "browsing_mode",
                    self.browsingMode
                )
                self.fileInfo = self.get_server_setting(
                    "show_file_info",
                    self.fileInfo
                )
                self.tempPath = self.get_server_setting(
                    "temp_path",
                    self.tempPath
                ).rstrip("/")
                self.timeout = self.get_server_setting(
                    "timeout",
                    self.timeout
                )
            else:
                self.forceReloadCat = False
            self.dontEditExt = self.get_settings().get(
                "dont_edit_ext",
                []
            )
            self.catExcludeFolders = self.get_settings().get(
                "cat_exclude_folders",
                []
            )
            if not quickConnect:
                self.server = self.servers[self.serverName]
                self.dontEditExt = self.get_server_setting(
                    "dont_edit_ext",
                    self.dontEditExt
                )
                self.catExcludeFolders = self.get_server_setting(
                    "cat_exclude_folders",
                    self.catExcludeFolders
                )
        except Exception as e:
            debug("Exception when gathering server settings for %s: %s" % (
                self.serverName, e
            ))
            self.serverName = None
            self.run()
            return
        # Open a connection to the server and present the user with filelist
        self.open_server()

    def open_server(self):
        reData = self.window.active_view().settings().get("reData", None)
        if reData and self.serverName == reData["serverName"]:
            if "browse_path" in reData:
                self.lastDir = reData["browse_path"]
            else:
                self.lastDir = reData["path"]
        elif not self.lastDir:
            self.lastDir = self.get_server_setting(
                "remote_path",
                "/home/%s" % self.get_server_setting("user")
            )
        # If we're not sftp only then see if we've gathered any info on this server
        if not self.get_server_setting("sftp_only") and not self.get_settings().get("%s:ls_version" % self.serverName):
            cmd = "echo $SHELL; grep --version; ls --version"
            self.run_ssh_command(cmd, callback=self.handle_server_info)
        else:
            self.check_cat()
            self.show_current_path_panel(doCat=False)

    def handle_server_info(self, results):
        if "host_unknown" in results:
            if sublime.ok_cancel_dialog(
                "IMPORTANT! This host has not been seen before, would you like to PERMANENTLY record its fingerprint for later connections?",
                "Yes, store the server fingerprint"
            ):
                cmd = "echo $SHELL; grep --version; ls --version"
                self.run_ssh_command(cmd, callback=self.handle_server_info, acceptNew=True)
        else:
            # Check we succeeded:
            if not results["success"] or not results["out"]:
                # Check we haven't done this before
                try:
                    if self.triedAgain:
                        return self.error_message("Error connecting to %s to collect basic information. Please check your settings (especially the hostname), restart sublime and try again. If this fails, please report the error." % self.serverName)
                except:
                    # try once more...
                    self.triedAgain = True
                    cmd = "echo $SHELL; grep --version; ls --version"
                    return self.run_ssh_command(cmd, callback=self.handle_server_info)
            # Firstly, which ls do I have?
            if "ls: illegal option" in results["out"] or "ls: illegal option" in results["err"]:
                lsVersion = "UNIX"
            else:
                lsReg = re.compile("^ls\s.*\s([0-9]+\.[0-9]+)\s*$", re.MULTILINE)
                lsRes = re.search(lsReg, results["out"])
                if not lsRes:
                    # This will have to do for now, await more info
                    lsVersion = "UNIX"
                else:
                    lsVersion = lsRes.group(1)
            # Next, which grep
            grepReg = re.compile("^.*\s*grep\s*.*([0-9]+\.[0-9]+\.[0-9]+)\s*.*$", re.MULTILINE)
            grepRes = re.search(grepReg, results["out"])
            if not grepRes:
                grepVersion = "UNKNOWN"
            else:
                grepVersion = grepRes.group(1)
            # Lastly, which shell am I in?
            line = 1
            if results["out"].split("\n")[0][0] == "/":
                line = 0
            shellVersion = results["out"].split("\n")[line].strip().split("/")[-1]
            # Now save the settings back
            settings = {}
            settings["ls_version"] = lsVersion
            settings["grep_version"] = grepVersion
            settings["shell"] = shellVersion
            self.save_server_settings(self.serverName, settings)
            self.check_cat()
            self.show_current_path_panel()

    def save_server_settings(self, server, settings):
        sSettings = self.get_settings()
        for settingKey in settings:
            sSettings.set(self.serverName + ":" + settingKey, settings[settingKey])
        sublime.save_settings(self.settingFile)

    def handle_quick_host(self, cs):
        self.server = {}
        self.server["settings"] = {}
        ss = self.server["settings"]
        if "/" in cs:
            (cs, ss["remote_path"]) = cs.split("/", 1)
            ss["remote_path"] = "/" + ss["remote_path"]
        else:
            ss["remote_path"] = "/"
        if ":" in cs:
            (cs, ss["port"]) = cs.split(":")
        else:
            ss["port"] = "22"
        if "@" in cs:
            (ss["user"], ss["host"]) = cs.split("@")
        else:
            ss["user"] = "root"
            ss["host"] = cs

        self.serverName = ss["host"]
        self.show_input_panel(
            "Enter password (blank to attempt pageant auth: ",
            "",
            self.handle_quick_password,
            self.handle_change,
            self.handle_cancel
        )

    def handle_quick_password(self, password):
        self.server["settings"]["password"] = password
        self.start_server(self.serverName, True)

    def handle_fuzzy(self, selection):
        if selection != -1:
            (self.lastDir, selected) = self.split_path(self.items[selection][0])
            self.maintain_or_download(selected)

    def handle_grep(self, search):
        if not search:
            return self.show_quick_panel(self.items, self.handle_list)
        m = hashlib.md5()
        m.update(("%s%s%s" % (
            search,
            str(time.time()),
            self.lastDir
        )).encode('utf-8'))
        tmpFileName = "RE_%s.grep" % m.hexdigest()
        remotePath = "%s/%s" % (self.tempPath, tmpFileName)
        localPath = os.path.join(
            self.get_local_tmp_path(),
            tmpFileName
        )
        exclude = ""
        postExclude = ""
        if self.catExcludeFolders:
            if self.get_settings().get("%s:grep_version" % self.serverName, "1.0.0") < "2.5.2":
                for f in self.catExcludeFolders:
                    postExclude += "| grep -v \"%s\" " % f
            else:
                for f in self.catExcludeFolders:
                    exclude += "--exclude-dir=\"%s\" " % f
        # Direct the grep output to a file and download it to parse
        if "csh" in self.get_settings().get("%s:shell" % self.serverName):
            cmd = "cd %s && ( grep -i %s-nR -A2 -B2 \"%s\" . %s> %s) >&/dev/null;" % (
                self.escape_remote_path(self.lastDir),
                exclude,
                self.escape_remote_path(search),
                postExclude,
                self.escape_remote_path(remotePath)
            )
        else:
            cmd = "cd %s && grep -i %s-nR -A2 -B2 \"%s\" . %s> %s 2>/dev/null;" % (
                self.escape_remote_path(self.lastDir),
                exclude,
                self.escape_remote_path(search),
                postExclude,
                self.escape_remote_path(remotePath)
            )
        self.run_ssh_command(
            cmd,
            listenAttempts=2,
            callback=self.grep_callback_1,
            cP={"local": localPath, "remote": remotePath, "search": search}
        )

    def grep_callback_1(self, results, info):
        if not results["success"]:
            return self.error_message("Error searching remote server")
        # Make local folder to dave into
        try:
            os.makedirs(self.get_local_tmp_path())
        except FileExistsError:
            pass
        # Download results
        cmd = "get %s %s" % (
            self.escape_remote_path(info["remote"]),
            self.escape_remote_path(info["local"])
        )
        self.run_sftp_command(
            cmd,
            callback=self.grep_callback_2,
            cP=info
        )

    def grep_callback_2(self, results, info):
        if not results["success"]:
            return self.error_message("Error downloading remote server search")
        # Now delete remote file
        cmd = "rm %s" % (
            self.escape_remote_path(info["remote"])
        )
        self.run_sftp_command(cmd, dropResults=True)
        # Parse returned grep and display the results
        debug("Calling search results lister")
        self.window.run_command(
            "remote_edit_display_search",
            {
                "search": info["search"],
                "filePath": info["local"],
                "serverName": self.serverName,
                "baseDir": self.lastDir
            }
        )

    def handle_list(self, selection):
        if selection == -1:
            return
        selected = self.itemPaths[selection]
        if selection == 0:
            # text of server / dir
            caption = "Navigate to: "
            self.show_input_panel(
                caption,
                "%s" % self.lastDir,
                self.handle_navigate,
                self.handle_change,
                self.handle_cancel
            )
        elif selection == 1:
            # Options
            self.list_options()
        else:
            self.prevDir = self.lastDir
            # If we're going up a folder...
            if selection == 2:
                fileType = self.FILE_TYPE_FOLDER
            else:
                # Possibly a symlink, check that first...
                filePath = self.join_path(self.lastDir, selected)
                f = self.get_file_from_cat(filePath)
                # If we've loaded a new cat since listing this folder
                if not f or "/" not in f:
                    return self.navigate_unknown(filePath)
                fileType = f["/"][self.STAT_KEY_TYPE]
            if fileType == self.FILE_TYPE_SYMLINK:
                self.navigate_to_symlink(filePath, f)
            elif fileType == self.FILE_TYPE_FILE:
                self.maintain_or_download(selected)
            elif fileType == self.FILE_TYPE_FOLDER:
                if selection == 2:
                    if len(self.lastDir) <= 1:
                        self.lastDir = "/"
                    else:
                        (head, tail) = self.split_path(self.lastDir)
                        if len(head) <= 1:
                            self.lastDir = "/"
                        else:
                            self.lastDir = "%s/" % head
                else:
                    self.lastDir = self.join_path(
                        self.lastDir,
                        selected
                    )
                self.show_current_path_panel()
            else:
                # We should never get here!
                self.error_message("Navigation error!")

    def navigate_to_symlink(self, filePath, fileDict):
        try:
            (path, fileType) = self.get_symlink_dest(
                fileDict["/"][self.STAT_KEY_DESTINATION]
            )
        except:
            # We're likely in SFTP only mode as we should have this info in the
            # cat otherwise. We'll have to ls it and see what happens
            fileType = False
        if fileType == self.FILE_TYPE_FILE:
            (self.lastDir, self.selected) = self.split_path(path)
            self.maintain_or_download(path)
        elif fileType == self.FILE_TYPE_FOLDER:
            self.lastDir = path
            self.show_current_path_panel()
        else:
            self.navigate_unknown(path)

    def navigate_unknown(self, path):
        # We don't know where it links to, slap a slash on the end of it and
        # try to ls it...
        if path[-1] != "/":
            path += "/"
        cP = {}
        cP["path"] = path
        cP["prevDir"] = self.lastDir
        self.do_ls(path, self.unknown_callback_1, cP)

    def do_ls(self, path, callback, cP, acceptNew=False):
        if self.get_server_setting("sftp_only", False):
            params = ""
            if self.platform != "windows":
                params = "-la "
            cmd = "ls %s%s" % (
                params,
                self.escape_remote_path(path)
            )
            return self.run_sftp_command(
                cmd,
                callback=callback,
                cP=cP,
                acceptNew=acceptNew
            )
        else:
            cmd = "ls %s %s" % (
                self.get_ls_params(),
                self.escape_remote_path(path)
            )
            return self.run_ssh_command(
                cmd,
                callback=callback,
                cP=cP,
                acceptNew=acceptNew
            )

    def unknown_callback_1(self, results, cP):
        if not results["success"]:
            self.lastDir = cP["prevDir"]
            self.show_current_path_panel()
            self.error_message("Error navigating to %s" % cP["path"])
        elif "o such file or directory" in results["out"]:
            # sftp, either a file or doesn't exist. Do the same but go up a folder...
            # ls parent, check for file then prevDir or open file
            (head, tail) = self.split_path(cP["path"])
            self.do_ls(head, self.unknown_callback_2, cP)
        elif "Not a directory" in results["out"]:
            # It's a file.. open it
            self.maintain_or_download(cP["path"])
        else:
            # We should be a folder, parse the ls and add to the catalogue
            self.cat = self.parse_ls(
                self.cat,
                "./:\n%s" % (results["out"]),
                cP["path"]
            )
            self.lastDir = cP["path"]
            self.show_current_path_panel()

    def unknown_callback_2(self, results, cP):
        if not results["success"] or "o such file or directory" in results["out"]:
            self.lastDir = cP["prevDir"]
            self.show_current_path_panel()
            return self.error_message("Error navigating to %s" % cP["path"])
        else:
            # We can now check for the file / folder with:
            try:
                f = self.get_file_from_cat(cP["path"])
                fileType = f["/"][self.STAT_KEY_TYPE]
            except:
                self.lastDir = cP["prevDir"]
                self.show_current_path_panel()
                return self.error_message("Error navigating to %s" % cP["path"])
            if fileType == self.FILE_TYPE_SYMLINK:
                self.navigate_to_symlink(cP["path"], f)
            elif fileType == self.FILE_TYPE_FILE:
                self.maintain_or_download(cP["path"])

    def get_symlink_dest(self, path):
        d = self.get_file_from_cat(path)
        if d and "/" in d:
            if d["/"][self.STAT_KEY_TYPE] == self.FILE_TYPE_FOLDER:
                fileType = self.FILE_TYPE_FOLDER
            elif d["/"][self.STAT_KEY_TYPE] == self.FILE_TYPE_FILE:
                fileType = self.FILE_TYPE_FILE
            else:
                # Another symlink!
                return self.get_symlink_dest(d["/"][self.STAT_KEY_DESTINATION])
        else:
            fileType = False
        return (path, fileType)

    def maintain_or_download(self, selected):
        ext = selected.split(".")[-1]
        if self.browsingMode == "edit" and ext not in self.dontEditExt:
            self.download_and_open(selected)
        else:
            # give options
            # rename, chmod, chown, delete
            downloadFolder = self.get_default_download_folder()
            items = [
                ["» Edit '%s'" % selected],
                ["» Rename '%s'" % selected],
                ["» Move '%s'" % selected],
                ["» Copy '%s'" % selected],
                ["» Save to %s" % downloadFolder],
                ["» Save to %s and open" % downloadFolder],
                ["» Zip '%s' (and optionally download)" % selected],
                ["» chmod '%s'" % selected],
                ["» chown '%s'" % selected],
                ["» tail '%s' in a new tab" % selected],
                ["» Delete '%s'" % selected]
            ]
            # Show the options
            self.selected = selected
            self.show_quick_panel(items, self.handle_maintenance)

    def get_default_download_folder(self):
        ddf = self.get_settings().get("default_download_folder")
        if self.platform == "windows":
            return os.path.expandvars(
                self.get_settings().get(
                    "default_download_folder",
                    "%UserProfile%\\Downloads"
                )
            )
        else:
            if "%UserProfile%" in ddf:
                ddf = "~/Downloads"
            return os.path.expanduser(ddf)

    def handle_maintenance(self, selection):
        if selection == -1:
            self.show_current_path_panel()
        elif selection == 0:
            self.download_and_open(self.selected)
        elif selection == 1:
            # Rename
            caption = "Rename to: "
            self.show_input_panel(
                caption,
                self.selected,
                self.handle_rename,
                self.handle_change,
                self.show_list
            )
        elif selection == 2:
            # Move
            self.movingFrom = self.lastDir
            self.handle_move(-2)
        elif selection == 3:
            # Copy
            self.movingFrom = self.lastDir
            self.handle_copy(-2)
        elif selection == 4 or selection == 5:
            # Save file to download folder
            downloadFolder = self.get_default_download_folder()
            self.download_file_to(self.selected, downloadFolder, bool(selection == 5))
        elif selection == 6:
            # Zip
            items = [
                "Compress '%s' with bzip2 (.tar.bz2)" % self.selected,
                "Compress '%s' with gzip (.tar.gz)" % self.selected,
                "Compress '%s' with zip (.zip)" % self.selected,
                "Compress '%s' with lzma (.tar.xz)" % self.selected
            ]
            self.show_quick_panel(items, self.handle_compress)
        elif selection == 7:
            # chmod
            caption = "chmod to: "
            perms = self.get_perms(self.join_path(self.lastDir, self.selected))
            self.show_input_panel(
                caption,
                perms,
                self.handle_chmod,
                self.handle_change,
                self.show_list
            )
        elif selection == 8:
            # chown
            caption = "chown to: "
            (user, group) = self.get_user_and_group(
                self.join_path(self.lastDir, self.selected)
            )
            self.show_input_panel(
                caption,
                "%s:%s" % (user, group),
                self.handle_chown,
                self.handle_change,
                self.show_list
            )
        elif selection == 9:
            # tail
            path = self.join_path(self.lastDir, self.selected)
            self.connector.tail(
                path,
                self.escape_remote_path(path),
                self.serverName,
                self.server["settings"]
            )
        elif selection == 10:
            # Delete
            if sublime.ok_cancel_dialog(
                "Are you sure you want to delete %s?" % self.selected,
                "Delete"
            ):
                remotePath = self.join_path(self.lastDir, self.selected)
                cmd = "rm %s" % (
                    self.escape_remote_path(
                        remotePath
                    )
                )
                cP = {}
                cP["fileName"] = self.selected
                cP["fileDirectoryPath"] = self.lastDir
                self.run_sftp_command(
                    cmd,
                    callback=self.delete_file_callback,
                    cP=cP
                )

    def delete_file_callback(self, results, cP):
        if not results["success"] or (self.platform == "windows" and ": OK" not in results["out"]):
            if "permission denied" in results["out"]:
                return self.error_message(
                    "Permission denied error when trying to delete file %s" % cP["fileName"]
                )
            return self.error_message(
                "Error deleting file %s" % cP["fileName"]
            )
        f = self.get_file_from_cat(cP["fileDirectoryPath"])
        del f[self.selected]
        self.lastDir = cP["fileDirectoryPath"]
        self.show_current_path_panel()

    def delete_folder_callback(self, results, cP):
        if not results["success"] or (self.platform == "windows" and ": OK" not in results["out"]):
            if "permission denied" in results["out"]:
                return self.error_message(
                    "Permission denied error when trying to delete folder %s" % cP["folderName"]
                )
            return self.error_message(
                "Error deleting folder %s. Make sure it is empty first" % cP["folderName"]
            )
        f = self.get_file_from_cat(cP["folderDirectoryPath"])
        del f[cP["folderName"]]
        self.lastDir = cP["folderDirectoryPath"]
        self.show_current_path_panel()

    def handle_rename(self, fileName):
        if self.selected is -1:
            (head, tail) = self.split_path(self.lastDir)
        else:
            head = self.lastDir
            tail = self.selected
        if tail != fileName:
            oldPath = self.join_path(head, tail)
            newPath = self.join_path(head, fileName)
            cmd = "rename %s %s" % (
                self.escape_remote_path(oldPath),
                self.escape_remote_path(newPath)
            )
            cP = {}
            cP["oldName"] = tail
            cP["parentFolder"] = head
            cP["newName"] = fileName
            cP["oldPath"] = oldPath
            cP["newPath"] = newPath
            cP["selected"] = self.selected
            cP["lastDir"] = self.lastDir
            self.run_sftp_command(cmd, callback=self.rename_callback, cP=cP)
        else:
            self.show_quick_panel(self.items, self.handle_list)

    def rename_callback(self, results, cP):
        if results["success"]:
            self.success_message("File %s renamed to %s" % (
                cP["oldName"],
                cP["newName"]
            ))
        else:
            self.error_message("Error renaming file %s to %s" % (
                cP["oldName"],
                cP["newName"]
            ))
        if cP["selected"] is -1:
            self.lastDir = cP["newPath"]
        else:
            self.lastDir = cP["lastDir"]
        f = self.get_file_from_cat(cP["parentFolder"])
        f[cP["newName"]] = f[cP["oldName"]]
        del f[cP["oldName"]]
        self.show_current_path_panel()

    def handle_compress(self, selection):
        if selection == -1:
            return self.show_quick_panel(self.items, self.handle_list)
        elif selection == 0:
            ext = "tar.bz2"
        elif selection == 1:
            ext = "tar.gz"
        elif selection == 2:
            ext = "zip"
        elif selection == 3:
            ext = "tar.xz"
        else:
            return self.error_message("Unknown compression method")
        self.selection = selection
        fileName = "%s.%s" % (self.split_path(self.lastDir)[1], ext)
        self.items = [
            "When complete download to %s" % (
                self.get_default_download_folder()
            ),
            "Don't download, leave on server at %s" % self.join_path(
                self.lastDir,
                fileName
            )
        ]
        self.show_quick_panel(self.items, self.handle_compress_action)

    def handle_compress_action(self, selection):
        download = False
        if selection == -1:
            return self.show_quick_panel(self.items, self.handle_list)
        elif selection == 0:
            download = True
        if self.selection == 0:
            ext = "tar.bz2"
            cmd = "tar --bzip2 -cf"
        elif self.selection == 1:
            ext = "tar.gz"
            cmd = "tar -czf"
        elif self.selection == 2:
            ext = "zip"
            cmd = "zip -rq"
        elif self.selection == 3:
            ext = "tar.xz"
            cmd = "tar --lzma -cf"
        fileName = "%s-%s.%s" % (
            self.split_path(self.lastDir)[1] if self.selected == "." else self.selected,
            time.strftime("%Y-%m-%d_%H.%M"),
            ext
        )
        compressTo = "%s/%s" % (
            self.tempPath,
            fileName
        )
        cmd = "cd %s && %s %s %s" % (
            self.escape_remote_path(self.lastDir),
            cmd,
            self.escape_remote_path(compressTo),
            self.selected
        )
        cP = {}
        cP["ext"] = ext
        cP["folder"] = self.lastDir
        cP["download"] = download
        cP["fileName"] = fileName
        cP["compressTo"] = compressTo
        self.run_ssh_command(
            cmd,
            listenAttempts=2,
            callback=self.compress_callback_1,
            cP=cP
        )

    def compress_callback_1(self, results, cP):
        if not results["success"]:
            debug("Error compressing folder %s" % cP["folder"])
            self.error_message("Error compressing \"%s\"" % cP["folder"])
            self.list_directory(cP["folder"])
        else:
            debug("Successfully compressed folder %s" % cP["folder"])
            if cP["download"]:
                downloadFolder = self.get_default_download_folder()
                localPath = self.join_path(
                    downloadFolder,
                    cP["fileName"]
                )
                cP["downloadFolder"] = downloadFolder
                cP["localPath"] = localPath
                cmd = "get %s %s" % (
                    self.escape_remote_path(cP["compressTo"]),
                    self.escape_remote_path(localPath)
                )
                self.run_sftp_command(cmd, callback=self.compress_callback_2, cP=cP)
            else:
                cmd = "rename %s %s" % (
                    self.escape_remote_path(cP["compressTo"]),
                    self.escape_remote_path("%s.%s" % (
                        self.join_path(
                            cP["folder"],
                            self.split_path(cP["folder"])[1]
                        ),
                        cP["ext"]
                    ))
                )
                # For some odd reason psftp fails to move a file from /tmp to
                # /home. Permissions and everything are fine, I just get an error
                # code 4 back from sftp and the error messsage "failure". This
                # is on debian stable. Needs more investigation, psftp should be
                # fine for this simple move operation.
                self.run_ssh_command(cmd, callback=self.compress_callback_3, cP=cP)

    def compress_callback_2(self, results, cP):
        if not results["success"]:
            debug("Error downloading file %s" % cP["compressTo"])
            self.error_message("File compressed successfully but download failed. Your compressed file is at \"%s\"" % cP["compressTo"])
        else:
            debug("Successfully downloaded file %s" % cP["localPath"])
            self.success_message("File %s downloaded successfully" % (
                cP["fileName"]
            ))
            # Tidy up after ourselves
            cmd = "rm %s" % (
                self.escape_remote_path(cP["compressTo"])
            )
            self.run_sftp_command(cmd, dropResults=True)
        self.lastDir = cP["folder"]
        self.show_current_path_panel()

    def compress_callback_3(self, results, cP):
        forceReload = False
        if not results["success"] or "failure" in results["out"]:
            self.error_message("Error moving compressed file to %s" % (
                cP["folder"]
            ))
            self.lastDir = cP["folder"]
            forceReload = True
        self.show_current_path_panel(forceReload=forceReload)

    def handle_chmod(self, chmod):
        if self.selected is -1:
            fileName = self.lastDir
        else:
            fileName = self.join_path(self.lastDir, self.selected)
        chRex = re.compile("^[0-7]{3}$")
        if not re.search(chRex, chmod):
            self.error_message("Invalid chmod value, must be 3 numbers, each with a value from 0 to 7")
            caption = "chmod to: "
            perms = self.get_perms(fileName)
            return self.show_input_panel(
                caption,
                perms,
                self.handle_chmod,
                self.handle_change,
                self.show_list
            )
        cmd = "chmod %s %s" % (
            chmod,
            self.escape_remote_path(fileName)
        )
        cP = {}
        cP["fileName"] = fileName
        cP["chmod"] = chmod
        self.run_sftp_command(cmd, callback=self.chmod_callback, cP=cP)

    def chmod_callback(self, results, cP):
        if not results["success"]:
            self.error_message("Error attempting to chmod %s to %s" % (
                cP["fileName"],
                cP["chmod"]
            ))
        elif "Operation not permitted" in results["out"]:
            self.error_message("You do not have permission to chmod %s to %s" % (
                cP["fileName"],
                cP["chmod"]
            ))
        else:
            f = self.get_file_from_cat(cP["fileName"])
            f["/"][self.STAT_KEY_PERMISSIONS] = int(str(cP["chmod"]), 8)
        self.show_current_path_panel()

    def handle_chown(self, chown):
        if self.selected is -1:
            fileName = self.lastDir
        else:
            fileName = self.join_path(self.lastDir, self.selected)
        # Chown is not available over sftp
        cmd = "chown %s %s" % (
            chown,
            self.escape_remote_path(fileName)
        )
        cP = {}
        cP["fileName"] = fileName
        cP["chown"] = chown
        self.run_ssh_command(cmd, callback=self.chown_callback, cP=cP)

    def chown_callback(self, results, cP):
        if not results["success"]:
            self.error_message("Error attempting to chown %s to %s" % (
                cP["fileName"],
                cP["chown"]
            ))
        elif "Operation not permitted" in results["out"]:
            self.error_message("You do not have permission to chown %s to %s" % (
                cP["fileName"],
                cP["chown"]
            ))
        else:
            return self.show_current_path_panel(forceReload=True)
        self.show_current_path_panel()

    def show_list(self):
        self.show_quick_panel(self.items, self.handle_list)

    def get_user_and_group(self, filePath):
        # Only works if in the catalogue but can only be called after displaying
        # a folder that would ensure it *is* in the catalogue
        stats = self.get_file_stats(filePath)
        user = self.cat["/CAT_DATA/"]["users"][stats[self.STAT_KEY_USER]]
        group = self.cat["/CAT_DATA/"]["groups"][stats[self.STAT_KEY_GROUP]]
        return (user, group)

    def get_perms(self, filePath):
        # Only works if in the catalogue but can only be called after displaying
        # a folder that would ensure it *is* in the catalogue
        stats = self.get_file_stats(filePath)
        return oct(stats[self.STAT_KEY_PERMISSIONS])[2:5]

    def get_file_stats(self, filePath):
        f = self.get_file_from_cat(filePath)
        return f["/"]

    def get_file_from_cat(self, filePath):
        tmp = self.cat
        try:
            for f in filter(bool, filePath.split("/")):
                tmp = tmp[f]
            return tmp
        except:
            return False

    def append_files_from_path(self, fileDict, filePath):
        for f in filter(self.remove_stats, fileDict):
            # Don't show files / folders if they begin with a dot and showHidden
            # is not enabled.
            if self.showHidden or (not self.showHidden and f[0] != "."):
                # If we have a file
                if fileDict[f]["/"][self.STAT_KEY_TYPE] == self.FILE_TYPE_FILE:
                    self.items.append([
                        self.join_path(filePath, f),
                        "%s  %s %s %s %s" % (
                            oct(fileDict[f]["/"][self.STAT_KEY_PERMISSIONS])[2:5],
                            self.cat["/CAT_DATA/"]["users"][fileDict[f]["/"][self.STAT_KEY_USER]],
                            self.cat["/CAT_DATA/"]["groups"][fileDict[f]["/"][self.STAT_KEY_GROUP]],
                            "" if fileDict[f]["/"][self.STAT_KEY_TYPE] == self.FILE_TYPE_FOLDER else " %s " % self.display_size(fileDict[f]["/"][self.STAT_KEY_SIZE]),
                            self.display_time(fileDict[f]["/"][self.STAT_KEY_MODIFIED])
                        )
                    ])
                else:
                    # Else, assume a folder and recurse into it
                    # TODO: Symlinks?
                    self.append_files_from_path(
                        fileDict[f],
                        self.join_path(filePath, f)
                    )

    def handle_navigate(self, path):
        f = self.get_file_from_cat(path)
        if f and "/" in f:
            if f["/"][self.STAT_KEY_TYPE] == self.FILE_TYPE_SYMLINK:
                self.navigate_to_symlink(path, f)
            elif f["/"][self.STAT_KEY_TYPE] == self.FILE_TYPE_FILE:
                (self.lastDir, selected) = self.split_path(path)
                self.maintain_or_download(selected)
            elif f["/"][self.STAT_KEY_TYPE] == self.FILE_TYPE_FOLDER:
                self.lastDir = path
                self.show_current_path_panel()
        else:
            self.navigate_unknown(path)

    def list_options(self):
        if self.fileInfo:
            self.options = [
                ["» Bookmarks", "Go To / Manage Bookmarks"],
                ["» Back", "Return To Previous Directory"],
                ["» Server / List Options", "Amend List Behaviour, Change Servers"],
                ["» Folder Options", "Search / Maintain Folder"],
                ["» Return to File / Folder List", "Back To Where You Were"]
            ]
        else:
            self.options = [
                "» Bookmarks",
                "» Back",
                "» Server / List Options",
                "» Folder Options",
                "» Return to File / Folder List"
            ]
        self.show_quick_panel(self.options, self.handle_options)

    def handle_options(self, selection):
        if selection == -1:
            # Back to prev list
            self.show_current_path_panel()
        elif selection == 0:
            # Bookmarks
            self.list_bookmarks()
        elif selection == 1:
            # Back
            if self.prevDir is None:
                self.serverName = None
                return self.run()
            self.lastDir = self.prevDir
            self.show_current_path_panel()
        elif selection == 2:
            # Server / List options
            self.list_server_options()
        elif selection == 3:
            # Folder options
            self.list_folder_options()
        elif selection == 4:
            # Folder options
            self.show_current_path_panel()

    def list_server_options(self):
        self.serverOptions = [
            "» %s extended file / folder info" % ("Hide" if self.fileInfo else "Display"),
            "» %s hidden files / folders" % ("Hide" if self.showHidden else "Show"),
            "» Disconnect from server '%s'" % self.serverName,
            "» Options - Selecting opens immediately%s" % (" [SELECTED]" if self.browsingMode == "edit" else ""),
            "» Options - Selecting shows maintenance menu%s" % (" [SELECTED]" if self.browsingMode == "maintenance" else ""),
            "» Sort by filename %s" % ("descending" if self.orderBy == self.SORT_BY_NAME and not self.orderReverse else "ascending"),
            "» Sort by extension %s" % ("descending" if self.orderBy == self.SORT_BY_EXT and not self.orderReverse else "ascending"),
            "» Sort by type (file/folder) %s" % ("- folders first" if self.orderBy == self.SORT_BY_TYPE and not self.orderReverse else "- files first"),
            "» Sort by size %s" % ("ascending" if self.orderBy == self.SORT_BY_SIZE and self.orderReverse else "descending"),
            "» Sort by last modified %s" % ("descending" if self.orderBy == self.SORT_BY_MODIFIED and not self.orderReverse else "ascending"),
            "» Refresh entire catalogue",
            "» Return to options"
        ]
        self.show_quick_panel(self.serverOptions, self.handle_server_options)

    def handle_server_options(self, selection):
        if selection == -1 or len(self.serverOptions) == selection + 1:
            # Back to prev list
            self.list_options()
        elif selection == 0:
            # Turn on / off extended file / folder info
            self.fileInfo = self.fileInfo is False
            self.show_current_path_panel()
        elif selection == 1:
            # Show / hide hidden files
            self.showHidden = self.showHidden is False
            self.show_current_path_panel()
        elif selection == 2:
            # Disconnect from this server
            self.serverName = None
            self.run()
        elif selection == 3:
            # edit mode
            self.browsingMode = "edit"
            self.show_current_path_panel()
        elif selection == 4:
            # maintenance mode
            self.browsingMode = "maintenance"
            self.show_current_path_panel()
        elif selection == 5:
            if self.orderBy == self.SORT_BY_NAME:
                self.orderReverse = False if self.orderReverse else True
            else:
                self.orderBy = self.SORT_BY_NAME
                self.orderReverse = False
            self.show_current_path_panel()
        elif selection == 6:
            if self.orderBy == self.SORT_BY_EXT:
                self.orderReverse = False if self.orderReverse else True
            else:
                self.orderBy = self.SORT_BY_EXT
                self.orderReverse = False
            self.show_current_path_panel()
        elif selection == 7:
            if self.orderBy == self.SORT_BY_TYPE:
                self.orderReverse = False if self.orderReverse else True
            else:
                self.orderBy = self.SORT_BY_TYPE
                self.orderReverse = False
            self.show_current_path_panel()
        elif selection == 8:
            if self.orderBy == self.SORT_BY_SIZE:
                self.orderReverse = False if self.orderReverse else True
            else:
                self.orderBy = self.SORT_BY_SIZE
                self.orderReverse = True
            self.show_current_path_panel()
        elif selection == 9:
            if self.orderBy == self.SORT_BY_MODIFIED:
                self.orderReverse = False if self.orderReverse else True
            else:
                self.orderBy = self.SORT_BY_MODIFIED
                self.orderReverse = False
            self.show_current_path_panel()
        elif selection == 10:
            # Refresh ls for entire catalogue
            self.bgCat = time.time()
            self.show_quick_panel(self.items, self.handle_list)
            self.cat_server()
        else:
            # we shouldn't ever hit this
            return

    def list_folder_options(self):
        (head, tail) = self.split_path(self.lastDir)
        self.folderOptions = [
            "» Fuzzy file name search in '%s'" % tail,
            "» Find in files in '%s'" % tail,
            "» Create a new file within '%s'" % tail,
            "» Create a new folder within '%s'" % tail,
            "» Rename folder '%s'" % tail,
            "» Move folder '%s'" % tail,
            "» Copy folder '%s'" % tail,
            "» Open new tab with '%s' contents" % tail,
            "» Zip contents of '%s' (and optionally download)" % tail,
            "» Chmod '%s'" % tail,
            "» Chown '%s'" % tail,
            "» Delete '%s' (must be empty)" % tail,
            "» Refresh this folder",
            "» Return to options"
        ]
        self.show_quick_panel(self.folderOptions, self.handle_folder_options)

    def handle_folder_options(self, selection):
        if selection == -1 or len(self.folderOptions) == selection + 1:
            # Back to prev list
            self.list_options()
        elif selection == 0:
            # Fuzzy file name from here
            # TODO: ONLY SHOW THIS IF WE HAVE A CATALOGUE
            self.items = []
            self.append_files_from_path(
                self.get_file_from_cat(self.lastDir),
                self.lastDir
            )
            self.show_quick_panel(self.items, self.handle_fuzzy)
        elif selection == 1:
            # Search within files from here
            caption = "Enter search term: "
            self.show_input_panel(
                caption,
                "",
                self.handle_grep,
                self.handle_change,
                self.show_list
            )
        elif selection == 2:
            # New file
            caption = "Enter file name: "
            self.show_input_panel(
                caption,
                "",
                self.handle_new_file,
                self.handle_change,
                self.show_list
            )
        elif selection == 3:
            # New folder
            caption = "Enter folder name: "
            self.show_input_panel(
                caption,
                "",
                self.handle_new_folder,
                self.handle_change,
                self.show_list
            )
        elif selection == 4:
            # Rename
            caption = "Enter new name: "
            self.selected = -1
            (head, tail) = self.split_path(self.lastDir)
            self.show_input_panel(
                caption,
                tail,
                self.handle_rename,
                self.handle_change,
                self.show_list
            )
        elif selection == 5:
            # Move
            (self.lastDir, self.selected) = self.split_path(self.lastDir)
            self.movingFrom = self.lastDir.rstrip("/")
            self.handle_move(-2)
        elif selection == 6:
            # Copy
            (self.lastDir, self.selected) = self.split_path(self.lastDir)
            self.movingFrom = self.lastDir.rstrip("/")
            self.handle_copy(-2)
        elif selection == 7:
            # List folder contents
            self.list_directory(self.lastDir, callback=self.list_folder_callback, forceReload=True)
        elif selection == 8:
            # Zip
            (head, tail) = self.split_path(self.lastDir)
            items = [
                "Compress '%s' with bzip2 (.tar.bz2)" % tail,
                "Compress '%s' with gzip (.tar.gz)" % tail,
                "Compress '%s' with zip (.zip)" % tail,
                "Compress '%s' with lzma (.tar.xz)" % tail
            ]
            self.selected = "."
            self.show_quick_panel(items, self.handle_compress)
        elif selection == 9:
            # chmod
            self.selected = -1
            caption = "chmod to: "
            perms = self.get_perms(self.lastDir)
            self.show_input_panel(
                caption,
                perms,
                self.handle_chmod,
                self.handle_change,
                self.show_list
            )
        elif selection == 10:
            # chown
            self.selected = -1
            caption = "chown to: "
            (user, group) = self.get_user_and_group(self.lastDir)
            self.show_input_panel(
                caption,
                "%s:%s" % (user, group),
                self.handle_chown,
                self.handle_change,
                self.show_list
            )
        elif selection == 11:
            # delete
            self.selected = -1
            (head, tail) = self.split_path(self.lastDir)
            if sublime.ok_cancel_dialog(
                "Are you sure you want to delete %s?" % tail,
                "Delete"
            ):
                cmd = "rmdir %s" % self.escape_remote_path(self.lastDir)
                cP = {}
                cP["folderName"] = tail
                cP["folderDirectoryPath"] = head
                self.run_sftp_command(
                    cmd,
                    callback=self.delete_folder_callback,
                    cP=cP
                )
        elif selection == 12:
            # Refresh ls for this folder
            self.show_current_path_panel(forceReload=True)
        else:
            # we shouldn't ever hit this
            return

    def list_folder_callback(self, results, cp):
        ls = ""
        for line in results["out"].split("\n")[1:-1]:
            ls += "%s\n" % line.strip()
        self.window.run_command(
            "remote_edit_list_folder",
            {
                "path": cp["folder"],
                "contents": ls
            }
        )

    def list_bookmarks(self):
        bookmarks = sublime.load_settings(self.bookmarksSettingsFile)
        serverBookmarks = bookmarks.get(self.serverName, [])
        self.items = []
        self.itemPaths = []
        if self.lastDir not in serverBookmarks:
            self.items = [
                ["» Add new bookmark at \"%s\"" % self.lastDir]
            ]
            self.itemPaths = ["ADD"]
        if len(serverBookmarks) > 0:
            self.items.append("» Edit a bookmark")
            self.items.append("» Delete a bookmark")
            self.itemPaths.append("EDIT")
            self.itemPaths.append("DEL")
        for b in serverBookmarks:
            self.items.append("Go to: %s" % b)
            self.itemPaths.append(b)
        self.items.append("» Return to options")
        self.itemPaths.append("/BACK/")
        self.show_quick_panel(self.items, self.handle_bookmarks_list)

    def handle_bookmarks_list(self, selection):
        if selection == -1 or len(self.items) == selection + 1:
            return self.list_options()
        selected = self.itemPaths[selection]
        if selected == "ADD":
            bookmarks = sublime.load_settings(self.bookmarksSettingsFile)
            serverBookmarks = bookmarks.get(self.serverName, [])
            serverBookmarks.append(self.lastDir.rstrip("/"))
            bookmarks.set(self.serverName, serverBookmarks)
            sublime.save_settings(self.bookmarksSettingsFile)
            self.show_current_path_panel()
        elif selected == "EDIT":
            self.items = []
            self.itemPaths = []
            bookmarks = sublime.load_settings(self.bookmarksSettingsFile)
            serverBookmarks = bookmarks.get(self.serverName, [])
            if len(serverBookmarks) == 0:
                self.error_message("No bookmarks to edit")
                self.list_bookmarks()
            else:
                for b in serverBookmarks:
                    self.items.append("Edit: %s" % b)
                    self.itemPaths.append(b)
                self.show_quick_panel(self.items, self.handle_bookmarks_edit)
        elif selected == "DEL":
            self.items = []
            self.itemPaths = []
            bookmarks = sublime.load_settings(self.bookmarksSettingsFile)
            serverBookmarks = bookmarks.get(self.serverName, [])
            if len(serverBookmarks) == 0:
                self.error_message("No bookmarks to delete")
                self.list_bookmarks()
            else:
                for b in serverBookmarks:
                    self.items.append("Delete: %s" % b)
                    self.itemPaths.append(b)
                self.show_quick_panel(self.items, self.handle_bookmarks_delete)
        else:
            self.handle_navigate(self.itemPaths[selection])

    def handle_bookmarks_edit(self, selection):
        if selection == -1:
            self.list_bookmarks()
        else:
            self.selected = self.itemPaths[selection]
            caption = "Edit bookmark: "
            self.show_input_panel(
                caption,
                self.selected,
                self.handle_bookmark_edit,
                self.handle_change,
                self.list_bookmarks
            )

    def handle_bookmark_edit(self, text):
        if text:
            bookmarks = sublime.load_settings(self.bookmarksSettingsFile)
            serverBookmarks = bookmarks.get(self.serverName, [])
            b = serverBookmarks.index(self.selected)
            serverBookmarks[b] = text
            bookmarks.set(self.serverName, serverBookmarks)
            sublime.save_settings(self.bookmarksSettingsFile)
        self.list_bookmarks()

    def handle_bookmarks_delete(self, selection):
        if selection != -1:
            selected = self.itemPaths[selection]
            bookmarks = sublime.load_settings(self.bookmarksSettingsFile)
            serverBookmarks = bookmarks.get(self.serverName, [])
            b = serverBookmarks.index(selected)
            del serverBookmarks[b]
            bookmarks.set(self.serverName, serverBookmarks)
            sublime.save_settings(self.bookmarksSettingsFile)
        self.list_bookmarks()

    def handle_new_file(self, fileName):
        if not fileName:
            self.show_quick_panel(self.folderOptions, self.handle_folder_options)
        else:
            # Make local folder
            localFolder = self.make_local_folder()
            if not localFolder:
                # Error message
                return sublime.error_message(
                    "Error creating local folder"
                )
            else:
                localFile = os.path.join(localFolder, fileName)
                reData = {
                    "serverName": self.serverName,
                    "fileName": fileName,
                    "path": self.lastDir,
                    "openedAt": time.time()
                }
                self.window.open_file(localFile)
                self.window.active_view().settings().set("reData", reData)

    def handle_new_folder(self, folderName):
        if folderName:
            cmd = "mkdir %s" % (
                self.escape_remote_path(
                    self.join_path(self.lastDir, folderName)
                )
            )
            cP = {}
            cP["folder"] = folderName
            cP["path"] = self.lastDir
            self.run_sftp_command(
                cmd,
                callback=self.new_folder_callback,
                cP=cP
            )
        else:
            self.show_quick_panel(self.items, self.handle_list)

    def new_folder_callback(self, results, cP):
        if not results["success"]:
            self.error_message("Error attempting to create folder %s in %s" % (
                cP["folder"],
                cP["path"]
            ))
        else:
            self.lastDir = self.join_path(
                cP["path"],
                cP["folder"]
            )
            # We do this to keep or catalogue up to date, we're not viewing
            # this list, we're viewing the contents of our new folder (empty)
            cP["sftpMode"] = self.get_server_setting("sftp_only", False)
            cP["folder"] = cP["path"]
            self.do_ls(cP["path"], callback=self.parse_list_only_callback, cP=cP)
            self.items = []
            self.add_options_to_items()
            self.show_quick_panel(self.items, self.handle_list)

    def handle_move(self, selection):
        if selection == -1:
            self.show_current_path_panel()
        elif selection == 0:
            # Move self.selected to self.lastDir
            if self.movingFrom == self.lastDir:
                self.error_message("Source and destination paths are the same (%s), unable to move" % self.lastDir)
            else:
                cmd = "rename %s %s" % (
                    self.escape_remote_path(self.join_path(
                        self.movingFrom,
                        self.selected
                    )),
                    self.escape_remote_path(self.lastDir.rstrip("/") + "/" + self.selected.strip("/"))
                )
                cP = {}
                cP["folder"] = self.movingFrom
                return self.run_sftp_command(cmd, callback=self.move_callback, cP=cP)
        else:
            if selection == -2:
                # Do nothing, just display the menus
                pass
            elif selection == 1:
                (self.lastDir, tail) = self.split_path(self.lastDir)
            else:
                self.lastDir = self.join_path(
                    self.lastDir,
                    self.itemPaths[selection]
                )
        self.list_directory(self.lastDir, foldersOnly=True, skipOptions=True)
        if self.fileInfo:
            (head, tail) = self.split_path(self.lastDir)
            self.items.insert(0, [
                "  ../",
                "  Up to %s" % head
            ])
        else:
            self.items.insert(0, "  ../")
        self.itemPaths.insert(0, "UP")
        if self.movingFrom == self.lastDir:
            if self.fileInfo:
                self.items.insert(0, [
                    "Showing %s/, select a path to move %s to" % (self.lastDir, self.selected),
                    ""
                ])
            else:
                self.items.insert(0, "Showing %s/, select a path to move %s to" % (self.lastDir, self.selected))
        else:
            if self.fileInfo:
                self.items.insert(0, [
                    "Move %s to %s/" % (self.selected, self.lastDir),
                    ""
                ])
            else:
                self.items.insert(0, "Move %s to %s/" % (self.selected, self.lastDir))
        self.itemPaths.insert(0, "MOVE")
        self.show_quick_panel(self.items, self.handle_move)

    def move_callback(self, results, cP):
        if not results["success"] or "no such file or directory" in results["out"]:
            self.error_message("Error moving %s" % cP["folder"])
        cP["sftpMode"] = self.get_server_setting("sftp_only", False)
        self.do_ls(cP["folder"], callback=self.parse_list_only_callback, cP=cP)
        return self.show_current_path_panel(forceReload=True)

    def handle_copy(self, selection):
        if selection == -1:
            self.show_current_path_panel()
        elif selection == 0:
            # Copy self.selected to self.lastDir
            if self.movingFrom == self.lastDir:
                self.error_message("Source and destination paths are the same (%s), unable to copy" % self.lastDir)
            else:
                dest = self.lastDir.rstrip("/") + "/"
                cmd = "cp -r %s %s" % (
                    self.escape_remote_path(self.join_path(
                        self.movingFrom,
                        self.selected
                    )),
                    self.escape_remote_path(dest)
                )
                cP = {}
                cP["folder"] = self.movingFrom
                cP["item"] = self.selected
                cP["dest"] = dest
                return self.run_ssh_command(cmd, listenAttempts=2, callback=self.copy_callback, cP=cP)
        else:
            if selection == -2:
                # Do nothing, just display the menus
                pass
            elif selection == 1:
                (self.lastDir, tail) = self.split_path(self.lastDir)
            else:
                self.lastDir = self.join_path(
                    self.lastDir,
                    self.itemPaths[selection]
                )
        self.list_directory(self.lastDir, foldersOnly=True, skipOptions=True)
        if self.fileInfo:
            (head, tail) = self.split_path(self.lastDir)
            self.items.insert(0, [
                "  ../",
                "  Up to %s" % head
            ])
        else:
            self.items.insert(0, "../")
        self.itemPaths.insert(0, "UP")
        if self.movingFrom == self.lastDir:
            if self.fileInfo:
                self.items.insert(0, [
                    "Showing %s/, select a path to copy %s to" % (self.lastDir, self.selected),
                    ""
                ])
            else:
                self.items.insert(0, "Showing %s/, select a path to copy %s to" % (self.lastDir, self.selected))
        else:
            if self.fileInfo:
                self.items.insert(0, [
                    "Copy %s to %s/" % (self.selected, self.lastDir),
                    ""
                ])
            else:
                self.items.insert(0, "Copy %s to %s/" % (self.selected, self.lastDir))
        self.itemPaths.insert(0, "COPY")
        self.show_quick_panel(self.items, self.handle_copy)

    def copy_callback(self, results, cP):
        if not results["success"] or "no such file or directory" in results["out"]:
            self.error_message("Error copying %s from %s to %s" % (
                cP["item"],
                cP["folder"],
                cP["dest"]
            ))
        cP["sftpMode"] = self.get_server_setting("sftp_only", False)
        self.do_ls(cP["folder"], callback=self.parse_list_only_callback, cP=cP)
        return self.show_current_path_panel(forceReload=True)

    def add_options_to_items(self):
        if self.fileInfo:
            (head, tail) = self.split_path(self.lastDir)
            self.items.insert(0, [
                "  ../",
                "  Up to %s" % head
            ])
            self.items.insert(0, [
                "» Options - %s mode - sort by %s" % (
                    self.browsingMode,
                    self.order_by_to_string()
                ),
                "Manage folder %s or change preferences" % self.lastDir
            ])
            self.items.insert(0, ["» %s:%s  " % (
                self.serverName,
                self.lastDir
            ), self.get_server_setting("host")])
        else:
            self.items.insert(0, "  ../")
            self.items.insert(0, "» Options - %s mode - sort by %s" % (
                self.browsingMode,
                self.order_by_to_string()
            ))
            self.items.insert(0, "» %s:%s" % (
                self.serverName,
                self.lastDir
            ))
        self.itemPaths.insert(0, "Nothing")
        self.itemPaths.insert(0, "to see")
        self.itemPaths.insert(0, "here")

    def download_and_open(self, f, lineNumber=None):
        if "/" in f:
            (self.lastDir, f) = self.split_path(f)
        localFolder = self.make_local_folder()
        if not localFolder:
            # error message
            self.lastErr = "Error creating local folder"
            return False
        remoteFile = self.join_path(self.lastDir, f)
        localFile = os.path.join(localFolder, self.escape_local_path(f))
        cmd = "get %s %s" % (
            self.escape_remote_path(remoteFile),
            self.escape_remote_path(localFile)
        )
        cP = {}
        cP["file"] = f
        cP["lastDir"] = self.lastDir
        cP["localFile"] = localFile
        cP["lineNumber"] = lineNumber
        cP["serverName"] = self.serverName
        self.run_sftp_command(cmd, callback=self.download_and_open_callback, cP=cP)

    def download_and_open_callback(self, results, cP):
        if not results["success"]:
            return self.error_message("Error downloading %s" % cP["file"])
        # These persist between app reloads. W00T W00T
        reData = {
            "serverName": cP["serverName"],
            "fileName": cP["file"],
            "path": cP["lastDir"],
            "openedAt": time.time()
        }
        self.window.open_file(cP["localFile"])
        self.window.active_view().settings().set("reData", reData)
        if cP["lineNumber"]:
            self.scroll_to(cP["lineNumber"])
        return True

    def scroll_to(self, lineNumber):
        view = self.window.active_view()
        if view.is_loading():
            sublime.set_timeout(
                lambda: self.scroll_to(lineNumber),
                50
            )
        else:
            view.run_command("goto_line", {"line": lineNumber})

    def download_file_to(self, f, destination, openFile):
        sourceFile = self.join_path(self.lastDir, f)
        destFile = os.path.join(
            destination,
            self.escape_local_path(f)
        )
        cmd = "get %s %s" % (
            self.escape_remote_path(sourceFile),
            self.escape_remote_path(destFile)
        )
        cP = {}
        cP["file"] = self.selected
        cP["open"] = openFile
        cP["destination"] = destination
        self.run_sftp_command(cmd, callback=self.download_file_callback, cP=cP)

    def download_file_callback(self, results, cP):
        if results["success"]:
            self.success_message("File %s downloaded to %s" % (
                cP["file"],
                cP["destination"]
            ))
        else:
            return self.error_message("Error downloading %s" % cP["file"])
        if cP["open"]:
            # And open
            f = os.path.join(
                cP["destination"],
                cP["file"]
            )
            os.startfile(f)

    def list_directory(self, d, dontLoop=False, forceReload=False, foldersOnly=False, skipOptions=False, callback=None, doCat=None, acceptNew=False):
        self.items = []
        self.itemPaths = []
        found = False
        sftpMode = self.get_server_setting("sftp_only", False)
        if not forceReload and self.cat:
            # Display options based on the catalogue and self.lastDir
            try:
                debug("Trying cat for folder \"%s\"" % d)
                found = True
                fldr = self.get_file_from_cat(d)
                if "/NO_INDEX/" in fldr:
                    # It's in our catalogue but we haven't listed contents yet
                    raise Exception("Path not in catalogue")
                # debug("D is: %s" % d)
                # debug("Fldr is: %s" % fldr)
                for f in sorted(filter(self.remove_stats, fldr), reverse=self.orderReverse, key=lambda x: fldr[x]["/"][self.orderBy] if self.orderBy not in [self.SORT_BY_NAME, self.SORT_BY_EXT] else (x.lower() if self.orderBy == self.SORT_BY_NAME else (x.split(".")[-1] if "." in x and fldr[x]["/"][self.STAT_KEY_TYPE] == self.FILE_TYPE_FILE else ("zzzzzz" + x if fldr[x]["/"][self.STAT_KEY_TYPE] == self.FILE_TYPE_FILE else "zzzzzzzzz" + x)))):
                    # debug("F is: %s" % f)
                    if self.showHidden or (not self.showHidden and f[0] != "."):
                        if fldr[f]["/"][self.STAT_KEY_TYPE] == self.FILE_TYPE_FOLDER:
                            fileName = "%s/" % f
                        elif fldr[f]["/"][self.STAT_KEY_TYPE] == self.FILE_TYPE_FILE:
                            fileName = f
                            if foldersOnly:
                                continue
                        else:
                            # A symlink
                            if sftpMode:
                                fileName = "%s (Symlink)" % (f)
                            else:
                                fileName = "%s (Symlink to: %s)" % (f, fldr[f]["/"][self.STAT_KEY_DESTINATION])
                            if foldersOnly:
                                continue
                        if self.fileInfo:
                            self.items.append(
                                [
                                    "  %s" % fileName,
                                    "  %s  %s %s %s %s" % (
                                        oct(fldr[f]["/"][self.STAT_KEY_PERMISSIONS])[2:5],
                                        self.cat["/CAT_DATA/"]["users"][fldr[f]["/"][self.STAT_KEY_USER]],
                                        self.cat["/CAT_DATA/"]["groups"][fldr[f]["/"][self.STAT_KEY_GROUP]],
                                        "" if fldr[f]["/"][self.STAT_KEY_TYPE] == self.FILE_TYPE_FOLDER else " %s " % self.display_size(fldr[f]["/"][self.STAT_KEY_SIZE]),
                                        self.display_time(fldr[f]["/"][self.STAT_KEY_MODIFIED])
                                    )
                                ]
                            )
                        else:
                            # debug("Key: %s, Val: %s" % (f, fldr[f]))
                            self.items.append("  %s" % fileName)
                        self.itemPaths.append(f)
            except Exception as e:
                debug("\"%s\" not in catalogue. Exception: %s" % (d, e))
                found = False
        if not found:
            if dontLoop:
                # We've already been around once and triggered an error
                # In theory this shouldn't happen but we need to inform the
                # user if it does
                return self.error_message(
                    "Recieved error when trying to list \"%s\"" % d
                )
            if d[-1] != "/":
                d += "/"
            cP = {}
            cP["folder"] = d
            cP["dontLoop"] = dontLoop
            cP["forceReload"] = forceReload
            cP["foldersOnly"] = foldersOnly
            cP["skipOptions"] = skipOptions
            cP["sftpMode"] = sftpMode
            cP["doCat"] = doCat
            if not callback:
                callback = self.list_directory_callback
            return self.do_ls(d, callback, cP, acceptNew=acceptNew)
        reData = self.window.active_view().settings().get("reData", None)
        if reData and self.serverName == reData["serverName"]:
            reData["browse_path"] = self.lastDir
            self.window.active_view().settings().set("reData", reData)
        if not skipOptions:
            self.add_options_to_items()
        # Callback set but unused, we just need to display the list
        if callback:
            self.show_quick_panel(self.items, self.handle_list)
            if doCat:
                self.check_cat()
        return True

    def parse_list_only_callback(self, results, cP=None):
        if "Not a directory" not in results["out"] and "Permission denied" not in results["out"]:
            # Parse the ls and add to the catalogue (but don't save the file)
            self.cat = self.parse_ls(
                self.cat,
                "./:\n%s" % (results["out"]),
                cP["folder"],
                sftpMode=cP["sftpMode"]
            )

    def list_directory_callback(self, results, cP=None, calledBack=True):
        if not results["success"]:
            if "host_unknown" in results:
                if sublime.ok_cancel_dialog(
                    "IMPORTANT! This host has not been seen before, would you like to PERMANENTLY record its fingerprint for later connections?",
                    "Yes, store the server fingerprint"
                ):
                    self.list_directory(self.lastDir, forceReload=cP["forceReload"], callback=self.list_directory_callback, doCat=cP["doCat"], acceptNew=True)
            else:
                return self.error_message(
                    "Error listing folder %s" % cP["folder"]
                )
        elif "No such file or directory" in results["out"]:
            # Dir not found, up one and try again
            (self.lastDir, tail) = self.split_path(self.lastDir)
            self.error_message("Directory %s not found, trying %s" % (
                cP["folder"],
                self.lastDir
            ))
            return self.list_directory(
                self.lastDir,
                forceReload=cP["forceReload"],
                callback=self.list_directory_callback,
                doCat=cP["doCat"]
            )
        elif "Not a directory" in results["out"]:
            # Make sure we have details of the file
            (head, tail) = self.split_path(cP["folder"])
            self.list_directory(
                head,
                dontLoop=cP["dontLoop"],
                forceReload=cP["forceReload"],
                foldersOnly=cP["foldersOnly"],
                skipOptions=cP["skipOptions"]
            )
            return False
        elif "Permission denied" in results["out"]:
            self.error_message(
                "Permission denied when trying to access %s" % cP["folder"]
            )
            (self.lastDir, tail) = self.split_path(cP["folder"])
            return self.show_current_path_panel()
        # Parse the ls and add to the catalogue (but don't save the file)
        self.cat = self.parse_ls(
            self.cat,
            "./:\n%s" % (results["out"]),
            cP["folder"],
            sftpMode=cP["sftpMode"]
        )
        s = self.list_directory(
            cP["folder"],
            dontLoop=True,
            forceReload=False,
            foldersOnly=cP["foldersOnly"],
            skipOptions=cP["skipOptions"]
        )
        if not calledBack:
            return s
        # Show the options
        self.show_quick_panel(self.items, self.handle_list)
        if cP["doCat"]:
            self.check_cat()

    def show_current_path_panel(self, forceReload=False, doCat=True):
        self.list_directory(self.lastDir, forceReload=forceReload, callback=self.list_directory_callback, doCat=doCat)

    def remove_stats(self, f):
        return False if not f or "/" in f else True

    def display_time(self, uTime, format="%Y-%m-%d %H:%M"):
        return time.strftime(format, time.localtime(uTime))

    def display_size(self, bytes):
        """Thanks to https://pypi.python.org/pypi/hurry.filesize/"""
        traditional = [
            (1024 ** 5, 'P'),
            (1024 ** 4, 'T'),
            (1024 ** 3, 'G'),
            (1024 ** 2, 'M'),
            (1024 ** 1, 'K'),
            (1024 ** 0, 'B'),
        ]
        for factor, suffix in traditional:
            if bytes >= factor:
                break
        return str(int(bytes/factor)) + suffix

    def check_cat(self):
        # If it's already set and we don't need to reload then BFN.
        if self.cat and not self.forceReloadCat:
            return
        # If it's disabled then ta-ra!.
        if not self.get_server_setting("enable_cat"):
            return
        # If we don't have a catalogue path then see you next tuesday.
        if not self.get_server_setting("cat_path"):
            return
        # If we're set for sftp_only then we can't recursively ls
        if self.get_server_setting("sftp_only"):
            return

        # First, see if we've already got a catalogue
        catPath = self.get_cat_path()
        self.catFile = os.path.join(
            catPath,
            "%s.cat" % self.serverName
        )
        if not os.path.exists(catPath):
            try:
                os.makedirs(catPath)
            except:
                pass
        try:
            mTime = os.path.getmtime(self.catFile)
            stale = self.get_settings().get("cat_stale_after_hours", 24) * 3600
        except:
            mTime = stale = 0
        # And it's recent...
        if mTime + stale < time.time():
            # Needs a refresh, leave our current catalogue where it is for now
            # though as it's likely better than nothing and we know to update it
            # Let's see if we're already cataloguing...
            catTimeout = 60 * 3
            if self.bgCat and self.bgCat + catTimeout > time.time():
                debug("Already cataloguing. No need to restart.")
                return
            # Use the below flag to indicate we will be cataloguing in the BG
            self.bgCat = time.time()
            # Create an extra ssh and sftp thread so that we don't interrupt
            # the user browsing the server (which is likely what triggered the
            # catalogue download in the first place).
            # self.create_ssh_thread()
            # self.create_sftp_thread()
            ## GO!
            self.cat_server()
        self.load_cat()
        # TODO: check bgCat for X minx in past, if too long then trigger the
        # download again

    def cat_server(self, results=None, cp=None):
        # TODO set a flag for known hosts after first connect
        # if the flag is set we can punt the plink query straight into the
        # background thread without worrying about known hosts
        if not cp:
            self.bgCatStep = 0
            cp = {}
            cp["server"] = self.serverName
            cp["step"] = 0
        debug("Cat server called for %s, step is %s" % (
            cp["server"],
            self.bgCatStep
        ))

        # TODO: Replace magic numbers
        # Put in some retrys
        # Make individual bits callable manually (refresh from... etc)

        if cp["server"] != self.serverName:
            return self.tidy_cat_process()

        # STEP 1
        if self.bgCatStep is 0:
            # TODO, Much better error messages
            self.bgCatStep += 1
            cp["server"] = self.serverName
            cp["step"] = self.bgCatStep

            self.catFile = os.path.join(
                self.get_cat_path(),
                "%s.cat" % self.serverName
            )
            try:
                os.makedirs(self.get_local_tmp_path())
            except FileExistsError:
                # Directory already exists
                pass
            except Exception as e:
                debug("Exception when making local folder: %s" % e)
                return False

            # We should be at a prompt
            if "csh" in self.get_settings().get("%s:shell" % self.serverName):
                cmd = "cd %s && ( ls %s -R > %s/%sSub.cat ) >&/dev/null; cd %s && rm %sSub.tar.gz; tar cfz %sSub.tar.gz %sSub.cat && rm %sSub.cat" % (
                    self.escape_remote_path(self.get_server_setting("cat_path")),
                    self.get_ls_params(),
                    self.escape_remote_path(self.tempPath),
                    self.serverName,
                    self.escape_remote_path(self.tempPath),
                    self.serverName,
                    self.serverName,
                    self.serverName,
                    self.serverName
                )
            else:
                cmd = "cd %s && ls %s -R > %s/%sSub.cat 2>/dev/null; cd %s && rm %sSub.tar.gz; tar cfz %sSub.tar.gz %sSub.cat && rm %sSub.cat" % (
                    self.escape_remote_path(self.get_server_setting("cat_path")),
                    self.get_ls_params(),
                    self.escape_remote_path(self.tempPath),
                    self.serverName,
                    self.escape_remote_path(self.tempPath),
                    self.serverName,
                    self.serverName,
                    self.serverName,
                    self.serverName
                )
            self.run_ssh_command(
                cmd,
                listenAttempts=10,
                callback=self.cat_server,
                cP=cp
            )
        elif self.bgCatStep is 1:
            if cp["step"] is not 1:
                return self.tidy_cat_process()
            if not results["success"]:
                return self.tidy_cat_process()
            self.bgCatStep += 1
            cp["server"] = self.serverName
            cp["step"] = self.bgCatStep
            cp["remoteCatGzPath"] = "%s/%s" % (self.tempPath, "%sSub.tar.gz" % self.serverName)
            cp["localCatGzPath"] = os.path.join(
                self.get_local_tmp_path(),
                "%sSub.tar.gz" % self.serverName
            )
            # Now grab the file
            cmd = "get %s %s" % (
                self.escape_remote_path(cp["remoteCatGzPath"]),
                self.escape_remote_path(cp["localCatGzPath"])
            )
            self.run_sftp_command(
                cmd,
                callback=self.cat_server,
                cP=cp
            )

        elif self.bgCatStep is 2:
            if cp["step"] is not 2:
                return self.tidy_cat_process()
            if not results["success"]:
                return self.tidy_cat_process()
            self.bgCatStep += 1
            cp["server"] = self.serverName
            cp["step"] = self.bgCatStep

            # delete tmp file from server
            cmd = "rm %s" % (
                self.escape_remote_path(cp["remoteCatGzPath"])
            )
            self.run_sftp_command(
                cmd,
                callback=self.cat_server,
                cP=cp
            )
        elif self.bgCatStep is 3:
            # split this one up more
            if cp["step"] is not 3:
                return self.tidy_cat_process()
            if not results["success"]:
                return self.tidy_cat_process()
            self.bgCatStep += 1
            cp["server"] = self.serverName
            cp["step"] = self.bgCatStep

            # Check local file exists
            try:
                f = tarfile.open(cp["localCatGzPath"], "r:gz")
                f.extractall(self.get_local_tmp_path())
            except Exception as e:
                debug("Gzip fail: \"%s\"" % e)
                return False
            finally:
                f.close()
            lsDataFile = os.path.join(
                self.get_local_tmp_path(),
                "%sSub.cat" % self.serverName
            )
            cat = self.create_cat(
                lsDataFile,
                self.get_server_setting("cat_path")
            )
            # Delete the local files we downloaded and untarred
            os.remove(cp["localCatGzPath"])
            os.remove(lsDataFile)
            # Save our python dict catalogue by pickleing it in some tangy,
            # slightly sweet, pickling vinegar.
            f = open(self.catFile, "wb")
            # Pickled egg?
            pickle.dump(cat, f)
            # Yes please! Don't mind if I do.
            f.close()
            debug("Catalogued. :)")
            # Now load it!
            self.load_cat()

    def load_cat(self):
        if os.path.exists(self.catFile):
            debug("Loading catalogue from %s." % self.catFile)
            self.cat = pickle.load(open(self.catFile, "rb"))
            # Flag to indicate that a full cat is loaded
            self.cat["/CAT_DATA/"]["loaded"] = time.time()
            debug("I've reloaded! Catalogue loaded from disk.")
            self.forceReloadCat = False

    def tidy_cat_process(self, resultServer=None):
        # Remove the extra threads
        # TODO: Cleanup local files
        # reset all variables
        # retry's here too
        debug("Tidy")

    def create_cat(self, fileName, startAt):
        # Build our catalogue dictionary from one big recursive ls of the root
        # (or cat_path) folder. The structure of the dict will be something
        # like:
        #
        # cat["/CAT_DATA/"]["server"] = server name
        # cat["/CAT_DATA/"]["created"] = unixtime created
        # cat["/CAT_DATA/"]["updated"] = unixtime updated
        # cat["/CAT_DATA/"]["users"] = users dict int -> user name
        # cat["/CAT_DATA/"]["groups"] = groups dict int -> group name
        # cat["folder1"]["/"] = [list of stat info on folder 1]
        # cat["folder1"]["folder2"]["/"] = [list of stat info on folder 2]
        # cat["folder1"]["file1"]["/"] = [list of stat info on file 1]
        #
        # Stat info is a list of data:
        # [0] - 0 = file, 1 = folder, 2 = symlink (see self.FILE_TYPE_FILE etc)
        # [1] - convert to octal for file perms
        # [2] - key of user dict to convert this id to a string user name
        # [3] - key of group dict to convert this id to a string group name
        # [4] - filesize in bytes
        # [5] - date as unixtime
        # [6] - if it's a symlink then record the full destination path
        cat = {}
        catFile = open(fileName, "r", encoding="utf-8", errors="ignore")
        cat = self.parse_ls(cat, catFile.read(), startAt)
        catFile.close()
        return cat

    def parse_ls(self, cat, lsData, startAt, users=[], groups=[], sftpMode=False):
        # Build a lookup dict for quickly converting rwxrwxrwx to an integer
        if not self.permsLookup:
            tmp = {}
            tmp["---"] = 0
            tmp["--x"] = 1
            tmp["-w-"] = 2
            tmp["-wx"] = 3
            tmp["r--"] = 4
            tmp["r-x"] = 5
            tmp["rw-"] = 6
            tmp["rwx"] = 7
            self.permsLookup = {}
            for x in tmp:
                for y in tmp:
                    for z in tmp:
                        self.permsLookup["%s%s%s" % (x, y, z)] = int(
                            "%s%s%s" % (
                                tmp[x],
                                tmp[y],
                                tmp[z]
                            ), 8)
        if "/CAT_DATA/" in cat and "users" in cat["/CAT_DATA/"]:
            users = cat["/CAT_DATA/"]["users"]
            groups = cat["/CAT_DATA/"]["groups"]
        if len(cat) == 0:
            cat["/NO_INDEX/"] = True
        tmpCat = cat
        tmpStartCat = cat
        for f in filter(bool, startAt.split('/')):
            if f not in tmpStartCat:
                tmpStartCat[f] = {"/NO_INDEX/": True}
            tmpStartCat = tmpStartCat[f]
        f_f_fresh = False
        cdStats = None
        key = "/"
        options = {}
        charsIn1 = 0
        charsIn2 = 0
        nixSftp = sftpMode and self.platform != "windows"
        for line in lsData.split("\n"):
            line = line.strip()
            # If a folder is specified (ends in a colon)
            if line and line[-1] == ":":
                f_f_fresh = False
                # All our folders begin "./"
                key = line[2:-1]
                options = {}
                charsIn1 = 0
                charsIn2 = 0
                if not key:
                    key = "/"
                else:
                    for f in filter(bool, key.split("/")):
                        if f in self.catExcludeFolders:
                            f_f_fresh = True
                            continue
            elif not line:
                if f_f_fresh:
                    continue
                # Separator (between folder contents and next folder)
                # Put our dict of folder contents onto the main array
                tmpCat = tmpStartCat
                for f in filter(bool, key.split('/')):
                    if f not in tmpCat:
                        tmpCat[f] = {"/NO_INDEX/": True}
                    tmpCat = tmpCat[f]
                # Remove files that have been deleted since last cat
                toDel = []
                for o in filter(self.remove_stats, tmpCat):
                    if o not in options:
                        toDel.append(o)
                for o in toDel:
                    del tmpCat[o]
                for o in options:
                    tmpCat[o] = options[o]
                try:
                    del tmpCat["/NO_INDEX/"]
                except:
                    pass
                if cdStats:
                    tmpCat["/"] = cdStats
                cdStats = None
            elif self.get_ls_params() in line:
                # Skip our command
                continue
            else:
                if f_f_fresh:
                    continue
                # These are our folder contents, add them to a dict until
                # we hit a blank line which signifies the end of that list
                # Break the line on whitespace
                sl = line.split()
                # File / folder name is always the last item in the list
                name = sl[-1].rstrip("/")
                cName = None
                # As it may contain spaces we cheat to get the file name once
                # we have hit our "." current directory. Try to make this
                # fairly robust
                if not charsIn1 and name == "." and len(options) is 0:
                    charsIn1 = line.find("./")
                    cName = "/CURRENT/"
                # Verify that with the ".." up a dir
                elif not charsIn2 and name == ".." and len(options) is 0:
                    charsIn2 = line.find("../")
                elif len(sl) < 5:
                    # Skip the "Total BYTES" message
                    pass
                elif (not charsIn1 or not charsIn2 or charsIn1 != charsIn2) and not nixSftp:
                    debug("Error parsing ls output at line: \"%s\"" % line)
                else:
                    if nixSftp:
                        cName = name
                    else:
                        cName = line[charsIn1:].rstrip()
                    if not sftpMode and sl[0][0] == "l" and "->" in cName:
                        (cName, symlinkDest) = cName.split(" -> ")
                        # If the symlink destination starts with a / then it's
                        # absolute and so no need to calc a path. If not it's
                        # relative so we need to work out its absolute path.
                        if symlinkDest[0] != "/":
                            prepend = self.join_path(startAt, key)
                            if symlinkDest[0:2] == "./":
                                symlinkDest = symlinkDest[2:]
                            elif symlinkDest[0:3] == "../":
                                (symlinkDest, prepend) = self.up_dir_to_path(
                                    symlinkDest, prepend
                                )
                            symlinkDest = self.join_path(
                                prepend,
                                symlinkDest
                            )
                if len(sl) >= 7 and cName:
                    cName = cName.rstrip("/")
                    # If we have a full row of info and we're not a folder up
                    # (..) or current folder reference (.) then add to our dict
                    tmpT = sl[0][0]
                    if tmpT == "-":
                        t = self.FILE_TYPE_FILE
                    elif tmpT == "d":
                        t = self.FILE_TYPE_FOLDER
                    elif tmpT == "l":
                        t = self.FILE_TYPE_SYMLINK
                    elif tmpT in ["c", "b"]:
                        continue
                    else:
                        debug(
                            "Unknown type (\"d\", \"-\" etc) at line: \"%s\""
                            % line
                        )
                    try:
                        peaky = sl[0][1:10]
                        p = self.permsLookup[peaky]
                    except:
                        try:
                            peaky = peaky.replace("s", "x").replace("t", "x")
                            p = self.permsLookup[peaky]
                        except:
                            # SUIG, GUID and sticky bits should work, anything
                            # else will be skipped
                            debug(
                                "Can't parse perms at line: \"%s\""
                                % line
                            )
                            continue
                    if sl[2] not in users:
                        users.append(sl[2])
                    u = users.index(sl[2])
                    if sl[3] not in groups:
                        groups.append(sl[3])
                    g = groups.index(sl[3])
                    # Parse bytes
                    try:
                        s = int(sl[4])
                    except:
                        # Some distros alias a -h into the ls command resulting
                        # in human readable output. Here we parse this from
                        # KB, MB, GB and TB back into bytes.
                        if sl[4][-1] == "K":
                            s = int(float(sl[4][0:-1]) * 1024)
                        elif sl[4][-1] == "M":
                            s = int(float(sl[4][0:-1]) * 1024 * 1024)
                        elif sl[4][-1] == "G":
                            s = int(float(sl[4][0:-1]) * 1024 * 1024 * 1024)
                        elif sl[4][-1] == "T":
                            s = int(float(sl[4][0:-1]) * 1024 * 1024 * 1024 * 1024)
                        else:
                            debug("Error parsing file size in line: %s" % line)
                            s = 0
                    # If we're in sftp mode then the filename won't necessarily
                    # be at position X but *will* be the last in the list. Date
                    # will be either "Month Day Year" or "Month Day HH:MM" (the
                    # latter for the current year)
                    if sftpMode:
                        if cName != "/CURRENT":
                            cName = sl[-1]
                        if ":" in sl[7]:
                            dateTime = "%s-%s-%s %s:%s" % (
                                time.strftime("%Y"),
                                sl[5],
                                sl[6],
                                sl[7][0:2],
                                sl[7][3:5]
                            )
                        else:
                            dateTime = "%s-%s-%s 00:00" % (
                                sl[7],
                                sl[5],
                                sl[6]
                            )
                        d = int(time.mktime(time.strptime(
                            dateTime,
                            "%Y-%b-%d %H:%M"
                        )))
                    else:
                        try:
                            d = int(time.mktime(time.strptime(
                                "%s %s" % (sl[5], sl[6]),
                                "%Y-%m-%d %H:%M"
                            )))
                        except:
                            debug("Can't parse date at line: \"%s\"" % line)
                            continue
                    stats = [t, p, u, g, s, d]
                    # If we have a symlink
                    if not sftpMode and t is self.FILE_TYPE_SYMLINK:
                        stats.append(symlinkDest)
                    # debug("%s: %s" % (cName, str(stats))
                    if cName == "/CURRENT":
                        # We have current folder, stats can go straight on the dict
                        cdStats = stats
                        cName = None
                    else:
                        options[cName] = {}
                        options[cName]["/"] = stats
                        if t is self.FILE_TYPE_FOLDER:
                            # We use this to indicate that the contents of this
                            # folder have not yet been indexed (otherwise we wouldn't
                            # be able to differentiate between this and an empty
                            # folder).
                            options[cName]["/NO_INDEX/"] = True
        # Put our final dict of folder contents onto the main dict
        tmpCat = tmpStartCat
        for f in filter(bool, key.split('/')):
            if f not in tmpCat:
                tmpCat[f] = {"/NO_INDEX/": True}
            tmpCat = tmpCat[f]
        # Remove files that have been deleted since last cat
        toDel = []
        for o in filter(self.remove_stats, tmpCat):
            if o not in options:
                toDel.append(o)
        for o in toDel:
            del tmpCat[o]
        for o in options:
            if o in tmpCat:
                tmpCat[o]["/"] = options[o]["/"]
            else:
                tmpCat[o] = options[o]
        try:
            del tmpCat["/NO_INDEX/"]
        except:
            pass
        # add user and group shizzle
        if "/CAT_DATA/" not in cat:
            cat["/CAT_DATA/"] = {}
        cat["/CAT_DATA/"]["server"] = self.serverName
        if "created" not in cat["/CAT_DATA/"]:
            cat["/CAT_DATA/"]["created"] = int(time.time())
        cat["/CAT_DATA/"]["updated"] = int(time.time())
        cat["/CAT_DATA/"]["users"] = users
        cat["/CAT_DATA/"]["groups"] = groups
        return cat

    def up_dir_to_path(self, symlinkDest, prepend):
        # debug("To: %s, path: %s" % (symlinkDest, prepend))
        if len(prepend) > 1 and prepend[-1] == "/":
            prepend = prepend[0:-1]
        if symlinkDest[0:3] == "../":
            return self.up_dir_to_path(
                symlinkDest[3:],
                prepend[0:prepend.rfind("/")] + "/"
            )
        return (symlinkDest, prepend)

    def get_server_setting(self, key, default=None):
        try:
            val = self.server["settings"][key]
        except:
            val = default
        return val

    def parse_order_by_setting(self, setting):
        setting = str(setting).lower()
        value = self.SORT_BY_NAME
        if setting == "name":
            value = self.SORT_BY_NAME
        if setting == "extension":
            value = self.SORT_BY_EXT
        elif setting == "type":
            value = self.SORT_BY_TYPE
        elif setting == "size":
            value = self.SORT_BY_SIZE
        elif setting == "modified":
            value = self.SORT_BY_MODIFIED
        return value

    def order_by_to_string(self):
        value = ""
        if self.orderBy == self.SORT_BY_NAME:
            value = "name"
        elif self.orderBy == self.SORT_BY_EXT:
            value = "extension"
        elif self.orderBy == self.SORT_BY_SIZE:
            value = "size"
        elif self.orderBy == self.SORT_BY_MODIFIED:
            value = "modified time"
        if self.orderBy == self.SORT_BY_TYPE:
            if self.orderReverse:
                value += "folders / files"
            else:
                value += "files / folders"
        else:
            if self.orderReverse:
                value += " desc."
            else:
                value += " asc."
        return value

    def remove_comments(self, text):
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
            data = self.remove_comments(data)
            return json.loads(data, strict=False)
        except Exception as e:
            self.lastJsonifyError = "Error parsing JSON: %s" % str(e)
            debug(self.lastJsonifyError)
            return False

    def get_server_config_path(self):
        return os.path.join(
            sublime.packages_path(),
            "User",
            "RemoteEdit",
            "Servers"
        )

    def get_cat_path(self):
        return os.path.join(
            sublime.packages_path(),
            "User",
            "RemoteEdit",
            "Cats"
        )

    def get_settings(self):
        if not self.settings:
            self.settings = sublime.load_settings(self.settingFile)
        return self.settings

    def show_quick_panel(self, options, done):
        sublime.set_timeout(
            lambda: self.window.show_quick_panel(options, done),
            10
        )

    def show_input_panel(self, caption, initialtext, done, change, cancel):
        sublime.set_timeout(
            lambda: self.window.show_input_panel(
                caption,
                initialtext,
                done,
                change,
                cancel
            ),
            10
        )

    def handle_change(self, selection):
        return

    def handle_cancel(self):
        return

    def split_path(self, path):
        return os.path.split(path.rstrip("/"))

    def join_path(self, path, folder):
        if not path or path[-1] is not "/":
            path = path + "/"
        newPath = "%s%s" % (path, folder)
        return newPath.rstrip("/")

    def error_message(self, msg, useLastError=False):
        if useLastError and self.lastErr:
            return sublime.error_message(self.lastErr)
        sublime.error_message(msg)
        return False

    def success_message(self, msg):
        sublime.message_dialog(msg)
        return True

    def escape_remote_path(self, path):
        if " " in path:
            return '"%s"' % path.replace('"', '""')
        else:
            return path.replace('"', '""')

    def escape_local_path(self, path):
        replacements = [
            ["<", "{"],
            [">", "}"],
            [":", ";"],
            ["\"", "'"],
            ["/", "_"],
            ["\\", "_"],
            ["|", "_"],
            ["?", "~"],
            ["*", "+"]
        ]
        for r in replacements:
            path = path.replace(r[0], r[1])
        return path

    def get_local_tmp_path(self, includeServer=True):
        if includeServer:
            return os.path.join(
                os.path.expandvars("%temp%") if self.platform == "windows" else "/tmp",
                "RemoteEdit",
                self.serverName
            )
        return os.path.join(
            os.path.expandvars("%temp%") if self.platform == "windows" else "/tmp",
            "RemoteEdit"
        )

    def tidy_local_tmp_path(self, timeout=0.3, ignoreIfTouchedWithin=3600 * 10):
        # We set a timeout so as not to block the main thread for too long. It's
        # set relativey high at .3 of a second as a normal run takes under .002
        # at the very most so it's there as a last resort rather than something
        # that really needs to be worried about.
        startAt = time.time()
        expireAt = time.time() + timeout
        # First we gather what we have open. As only 1 sublime can run at once
        # we're safe to delete anything else. By default we leave anything
        # touched within the last 10 hours (10 * 3600 seconds)
        oldIfTouchedBefore = time.time() - ignoreIfTouchedWithin
        openFiles = []
        rootPath = self.get_local_tmp_path(False)
        if (self.platform == "windows" and "\\Temp\\" not in rootPath) or (self.platform != "windows" and rootPath[0:4] != "/tmp"):
            # Sanity check on the path as we really, really don't want this to
            # start looping through the wrong directory deleting files as it goes
            return self.error_message("tidy_local_tmp_path() detected that \"%s\" was not in the folder path to tidy and so exited early. Please investigate." % (
                "Temp" if self.platform == "windows" else "/tmp"
            ))
        for v in self.window.views():
            if v.settings().get("reData", None):
                openFiles.append(v.file_name())
        # Now let's go over the filesystem
        # We do the folders after everything else and we need to reverse them
        # so bung them in a list on first pass through
        folders = []
        for root, dirs, files in os.walk(rootPath):
            # files first...
            for f in files:
                fullPath = os.path.join(root, f)
                if fullPath not in openFiles:
                    if os.path.getmtime(fullPath) < oldIfTouchedBefore:
                        os.remove(fullPath)
                        debug("Deleting file: %s " % (fullPath))
            # Now directories
            for d in dirs:
                fullPath = os.path.join(root, d)
                folders.append(fullPath)
            # Check to see if we're past our timeout
            if expireAt < time.time():
                debug("tidy_local_tmp_path() is out of time, we took: %s seconds" % (time.time() - startAt))
                return
        folders.reverse()
        for d in folders:
            if expireAt < time.time():
                debug("tidy_local_tmp_path() is out of time, we took: %s seconds" % (time.time() - startAt))
                return
            try:
                os.rmdir(d)
                debug("Deleting empty folder: %s" % d)
            except:
                pass
        debug("tidy_local_tmp_path() finished in %s seconds" % (time.time() - startAt))

    def make_local_folder(self):
        # file selected, ensure local folder is available
        localFolder = self.get_local_tmp_path()
        for f in self.lastDir.split("/"):
            if f:
                localFolder = os.path.join(
                    localFolder,
                    f
                )
        try:
            os.makedirs(localFolder)
        except FileExistsError:
            pass
        return localFolder

    def get_arithmetic(self):
        if "csh" in self.get_settings().get("%s:shell" % self.serverName):
            return ("echo \"uneeq\"`expr 66666666 + 44444445`\"uneeq\"", "uneeq111111111uneeq")
        else:
            return ("echo \"uneeq\"$((66666666 + 44444445))\"uneeq\"", "uneeq111111111uneeq")

    def get_ls_params(self):
        if self.get_settings().get("%s:ls_version" % self.serverName) == "UNIX":
            return self.unixLsParams
        else:
            return self.lsParams

    def run_ssh_command(
        self,
        cmd,
        checkReturn=None,
        listenAttempts=5,
        timeout=None,
        callback=None,
        cP=None,
        dropResults=False,
        acceptNew=False
    ):
        if self.get_server_setting("sftp_only", False):
            return self.error_message("This method is not supported under sftp_only mode. You may enable /disable this setting in your per server settings file.")
        (q, a) = self.get_arithmetic()
        return self.connector.run_remote_command(
            "ssh",
            cmd + "; " + q,
            a,
            listenAttempts,
            timeout,
            callback,
            cP,
            dropResults,
            acceptNew,
            serverName=self.serverName,
            serverSettings=self.server["settings"]
        )

    def run_sftp_command(
        self,
        cmd,
        checkReturn=None,
        listenAttempts=5,
        timeout=None,
        callback=None,
        cP=None,
        dropResults=False,
        acceptNew=False
    ):
        return self.connector.run_remote_command(
            "sftp",
            cmd,
            checkReturn,
            listenAttempts,
            timeout,
            callback,
            cP,
            dropResults,
            acceptNew,
            serverName=self.serverName,
            serverSettings=self.server["settings"]
        )


class RemoteEditConnector(object):
    window = None
    appResults = {}
    sshQueue = None
    sshThreads = []
    sftpQueue = None
    sftpThreads = []
    timeout = 60

    def __init__(self, window):
        self.window = window
        # Fire up a ssh and sftp thread and queue. Will immediately block the
        # queue waiting on first job.
        if not self.sftpQueue:
            self.sftpQueue = queue.Queue()
        if not self.sshQueue:
            self.sshQueue = queue.Queue()
        if not self.sshThreads:
            self.create_ssh_thread()
        if not self.sftpThreads:
            self.create_sftp_thread()

    def create_sftp_thread(self):
        key = len(self.sftpThreads)
        self.sftpThreads.append(
            RemoteEditConnectionWorker.RemoteEditConnectionWorker()
        )
        self.sftpThreads[key].start()
        self.sftpThreads[key].config(
            key,
            "sftp",
            self.sftpQueue,
            self.appResults,
            sublime.platform()
        )

    def create_ssh_thread(self):
        key = len(self.sshThreads)
        self.sshThreads.append(
            RemoteEditConnectionWorker.RemoteEditConnectionWorker()
        )
        self.sshThreads[key].start()
        self.sshThreads[key].config(
            key,
            "ssh",
            self.sshQueue,
            self.appResults,
            sublime.platform()
        )

    def __del__(self):
        debug("__del__ called")
        self.remove_ssh_thread(len(self.sshThreads))
        self.remove_sftp_thread(len(self.sftpThreads))

    def remove_ssh_thread(self, threadsToRemove=1):
        tc = len(self.sshThreads)
        if tc > 0:
            index = tc - 1
            threadsToRemove -= 1
            debug("Popping ssh")
            self.sshThreads.pop()
            # We need to send tc * messages down the wire so that each thread
            # gets a copy of the message. The threadId of each thread is its
            # key on the list.
            for i in range(tc):
                self.sshQueue.put({"KILL": index})
            if threadsToRemove > 0:
                self.remove_ssh_thread(threadsToRemove)

    def remove_sftp_thread(self, threadsToRemove=1):
        tc = len(self.sftpThreads)
        if tc > 0:
            index = tc - 1
            threadsToRemove -= 1
            debug("Popping sftp")
            self.sftpThreads.pop()
            # We need to send tc * messages down the wire so that each thread
            # gets a copy of the message. The threadId of each thread is its
            # key on the list.
            for i in range(tc):
                self.sftpQueue.put({"KILL": index})
            if threadsToRemove > 0:
                self.remove_sftp_thread(threadsToRemove)

    def run_remote_command(
        self,
        appType,
        cmd,
        checkReturn,
        listenAttempts=5,
        timeout=None,
        callback=None,
        cP=None,
        dropResults=False,
        acceptNew=False,
        serverName=None,
        serverSettings=None,
        q=None
    ):
        debug("run %s command called with cmd: \"%s\"" % (
            appType,
            cmd
        ))
        if timeout is None:
            timeout = self.timeout
        expireTime = time.time() + timeout
        work = {}
        work["server_name"] = serverName
        work["settings"] = serverSettings
        work["cmd"] = cmd
        work["prompt_contains"] = checkReturn
        work["listen_attempts"] = listenAttempts
        work["drop_results"] = dropResults
        work["timeout"] = timeout
        work["accept_new_host"] = acceptNew
        work["queue"] = q
        # Generate a unique key to listen for results on
        m = hashlib.md5()
        m.update(("%s%s" % (cmd, str(time.time()))).encode('utf-8'))
        key = m.hexdigest()
        work["key"] = key
        if appType == "sftp":
            self.sftpQueue.put(work)
        else:
            self.sshQueue.put(work)
        debug("....now on the queue.....")
        if q:
            return
        elif callback:
            debug("Calling set_timeout to check for results")
            # TODO: This should be totally events driven, have a thread block
            # on a queue and on return of data call a callback. Once we've
            # moved at least a bit towards that from where we are now it should
            # be a much easier task. For now we'll just have to check the
            # results dict regularly with set timeouts.
            sublime.set_timeout(
                lambda: self.handle_callbacks(
                    key,
                    expireTime,
                    callback,
                    cP
                ),
                100
            )
            return
        elif dropResults:
            return
        while True:
            # Give the command a little longer to timeout (15s) as the time it
            # went on the queue wasn't necessarily the same time that it started
            # processing
            if time.time() > (expireTime + 15):
                debug("Timeout")
                return False
            if key in self.appResults:
                results = self.appResults[key]
                del self.appResults[key]
                debug("Result found for cmd: %s" % cmd)
                break
            else:
                time.sleep(0.1)
        if not callback:
            return results["success"]

    def handle_callbacks(self, key, expireTime, callback, cP, statusState=0, statusDir=1):
        before = statusState % 8
        after = 7 - before
        if not after:
            statusDir = -1
        elif not before:
            statusDir = 1
        statusState += statusDir
        self.window.active_view().set_status("remoteedit", "RemoteEdit [%s=%s]" % (" " * before, " " * after))
        if key in self.appResults:
            self.window.active_view().set_status("remoteedit", "")
            debug("Results found in callback handler, firing the callback")
            results = self.appResults[key]
            if results["prompt_contains"]:
                results["out"] = results["out"].replace(results["prompt_contains"], "")
            del self.appResults[key]
            if cP is None:
                callback(results)
            else:
                callback(results, cP)
        # Give the command a little longer to timeout (15s) as the time it
        # went on the queue wasn't necessarily the same time that it started
        # processing
        elif time.time() > (expireTime + 15):
            self.window.active_view().set_status("remoteedit", "")
            if cP is None:
                callback(
                    {"success": False, "out": "", "err": ""}
                )
            else:
                callback(
                    {"success": False, "out": "", "err": ""},
                    cP
                )
        else:
            sublime.set_timeout(
                lambda: self.handle_callbacks(
                    key,
                    expireTime,
                    callback,
                    cP,
                    statusState,
                    statusDir
                ),
                100
            )

    def tail(self, path, escapedPath, serverName, serverSettings):
        q = queue.Queue()
        tab = self.window.new_file()
        tab.set_scratch(True)
        tab.set_name("Tailing %s..." % path)
        reTailData = {}
        reTailData["path"] = path
        reTailData["pos"] = 0
        # TODO: How to kill the thread when the tab is closed??
        tab.settings().set("reTailData", reTailData)
        self.create_ssh_thread()
        self.run_remote_command(
            "ssh",
            "tail -f %s" % escapedPath,
            checkReturn="",
            serverName=serverName,
            serverSettings=serverSettings,
            q=q
        )
        sublime.set_timeout(
            lambda: self.tail_updater(
                tab.id(), q
            ), 1000
        )

    def tail_updater(self, viewId, q):
        try:
            data = q.get_nowait()
        except queue.Empty:
            data = ""
        if data:
            sublime.active_window().run_command(
                "remote_edit_tail",
                {"viewId": viewId, "data": data}
            )
        sublime.set_timeout(
            lambda: self.tail_updater(
                viewId, q
            ), 1000
        )


class RemoteEditTailCommand(sublime_plugin.TextCommand):
    def run(self, edit, viewId, data):
        view = None
        if self.view.id() == viewId:
            view = self.view
        else:
            for v in self.view.window().views():
                if v.id() == viewId:
                    view = v
                    break
        if not view:
            return sublime.error_message("Unable to locate tab to tail")
        reTailData = view.settings().get("reTailData")
        i = view.insert(edit, reTailData["pos"], self.tidy(data))
        reTailData["pos"] += i
        view.show(reTailData["pos"])
        view.settings().set("reTailData", reTailData)

    def tidy(self, text):
        return "\n".join(map(self.strip, text.split("\n")))

    def strip(self, text):
        return text.rstrip()


def plugin_loaded():
    sublime.active_window().run_command(
        "remote_edit",
        {"action": "on_app_start"}
    )


def debug(data):
    if len(data) > 3000:
        print("MAIN %s: %s" % (time.strftime("%H:%M:%S"), data[0:3000]))
    else:
        print("MAIN %s: %s" % (time.strftime("%H:%M:%S"), data))
