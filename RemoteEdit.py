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

    servers = {}
    serverName = None
    sshQueue = None
    appResults = {}
    sshThreads = []
    sftpQueue = None
    sftpThreads = []
    settings = None
    settingFile = "RemoteEdit.sublime-settings"
    bookmarksSettingsFile = "RemoteEditBookmarks.sublime-settings"
    cat = {}
    catFile = False
    forceReloadCat = True
    lastDir = None
    browsingMode = "edit"
    fileInfo = False
    showHidden = False
    dontEditExt = []
    catExcludeFolders = []
    bgCat = 0
    bgCatStep = 0
    permsLookup = None
    lsParams = "-lap --time-style=long-iso --color=never"
    orderBy = "name"
    orderReverse = False
    plinkPromptComtains = "$"
    psftpPromptContains = "psftp>"
    tempPath = "/tmp"
    timeout = 30
    FILE_TYPE_FILE = 0
    FILE_TYPE_FOLDER = 1
    FILE_TYPE_SYMLINK = 2
    SORT_BY_NAME = 10
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

    # Add links to find command results
    # Make sure temp name is unique
    # ESCAPING ESCAPING ESCAPING - add escaping function
    #
    # per server filename filters, add filters
    # filter by file size
    # filter by date modified
    #
    # Update progress of background recursive ls so that it can be picked up if it dies
    #
    # Copy directories to another location / server
    # set temp path
    # cat_stale_after_hours, refresh folder, refresh catalogue
    # server remote_path setting as dict + auto add to bookmarks.
    # Add new threads for cat creation.
    # Add sftp only option
    # download file list (ls)

    def run(self, save=None):
        # Ensure that the self.servers dict is populated
        self.load_server_list()
        # Fire up a ssh and sftp thread and queue. Will immediately block the
        # queue waiting on first job.
        if not self.sshQueue:
            self.create_queues()
        if not self.sshThreads:
            self.create_ssh_thread()
        if not self.sftpThreads:
            self.create_sftp_thread()
        if save:
            # Save called from external RE events handler class
            self.save(save)
        elif self.serverName:
            # Fire up the self.serverName server
            self.start_server(self.serverName)
        else:
            # List servers and startup options
            self.items = [name for name in sorted(self.servers)]
            items = [[
                "%s (%s)" % (name, self.servers[name]["settings"]["host"]),
                "User: %s, Path: %s" % (
                    self.servers[name]["settings"]["user"],
                    self.servers[name]["settings"]["remote_path"]
                )
            ] for name in self.items]
            items.insert(0, [
                " • Quick connect",
                "Just enter a host and a username / password"
            ])
            items.insert(0, [
                " • Add a new server",
                "Complete new server details to quickly connect in future"
            ])
            self.show_quick_panel(items, self.handle_server_select)

    def create_queues(self):
        # Set up our queues
        if not self.sftpQueue:
            self.sftpQueue = queue.Queue()
        if not self.sshQueue:
            self.sshQueue = queue.Queue()

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
            self.appResults
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
            self.appResults
        )

    def __del__(self):
        self.debug("__del__ called")
        self.remove_ssh_thread(len(self.sshThreads))
        self.remove_sftp_thread(len(self.sftpThreads))

    def remove_ssh_thread(self, threadsToRemove=1):
        tc = len(self.sshThreads)
        if tc > 0:
            index = tc - 1
            threadsToRemove -= 1
            self.debug("Popping ssh")
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
            self.debug("Popping sftp")
            self.sftpThreads.pop()
            # We need to send tc * messages down the wire so that each thread
            # gets a copy of the message. The threadId of each thread is its
            # key on the list.
            for i in range(tc):
                self.sftpQueue.put({"KILL": index})
            if threadsToRemove > 0:
                self.remove_sftp_thread(threadsToRemove)

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
            self.debug("File is being saved already")
            return
        # TODO: Set a status bar in progress symbol
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
        # Initiate the save
        cmd = "put %s %s" % (
            self.escape_remote_path(localFile),
            self.escape_remote_path(remoteFile)
        )
        if not self.run_sftp_command(cmd):
            self.release_lock(lockFile)
            # Mark view as dirty
            view = self.window.active_view()
            view.run_command("remote_edit_mark_dirty", {'id': v.id()})
            return self.command_error(cmd)
        # if succeeded then display ok
        if "permission denied" in self.lastOut:
            self.release_lock(lockFile)
            # Mark view as dirty
            view = self.window.active_view()
            view.run_command("remote_edit_mark_dirty", {'id': v.id()})
            return self.error_message(
                "Permission denied when attempting to write file %s to %s" % (
                    reData["fileName"],
                    serverName
                )
            )
        self.release_lock(lockFile)
        (path, fileName) = self.split_path(remoteFile)
        reData["remote_save"] = time.time()
        v.settings().set("reData", reData)
        msg = "File %s saved successfully to server %s" % (
            fileName,
            serverName
        )
        sublime.status_message(msg)
        self.debug(msg)
        # sublime.message_dialog(msg)

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
            # self.debug("%s, %s, %s" % (serverName, self.serverName, self.lastDir))
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
            self.debug("Exception when gathering server settings for %s: %s" % (
                self.serverName, e
            ))
            self.serverName = None
            self.run()
            return
        # Open a connection to the server and present the user with filelist
        # etc
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
        self.check_cat()
        self.show_current_path_panel(doCat=False)

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
        if selection == -1:
            self.show_current_path_panel()
        else:
            (self.lastDir, selected) = self.split_path(self.items[selection][1])
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
        if self.catExcludeFolders:
            for f in self.catExcludeFolders:
                exclude += "--exclude-dir=\"%s\" " % f
        # Direct the grep output to a file and download it to parse
        cmd = "cd %s && grep -i %s-nR -A2 -B2 \"%s\" . > %s 2>/dev/null; echo %s;" % (
            self.escape_remote_path(self.lastDir),
            exclude,
            self.escape_remote_path(search),
            self.escape_remote_path(remotePath),
            "$(((66666 + 44445) * 1000000 + (333333 * 3)))"
        )
        checkReturn = "111111999999"
        success = self.run_ssh_command(
            cmd,
            checkReturn=checkReturn,
            listenAttempts=2
        )
        if not success:
            return self.command_error(cmd)
        # Download results
        cmd = "get %s %s" % (
            self.escape_remote_path(remotePath),
            self.escape_remote_path(localPath)
        )
        success = self.run_sftp_command(cmd)
        if not success:
            return self.command_error(cmd)
        # Now delete remote file
        cmd = "del %s" % (
            self.escape_remote_path(remotePath)
        )
        self.run_sftp_command(cmd, dropResults=True)
        # Parse returned grep and display the results
        self.debug("Calling search results lister")
        self.window.run_command(
            "remote_edit_display_search",
            {
                "search": search,
                "filePath": localPath,
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
            # Folder options
            (head, tail) = self.split_path(self.lastDir)
            self.folderOptions = [
                " • Back to list",
                " • Disconnect from server '%s'" % self.serverName,
                " • Fuzzy file name search in '%s'" % tail,
                " • Search inside files in '%s'" % tail,
                " • Create a new file within '%s'" % tail,
                " • Create a new folder within '%s'" % tail,
                " • Rename folder '%s'" % tail,
                " • Move folder '%s'" % tail,
                " • Copy folder '%s'" % tail,
                " • List folder '%s' contents" % tail,
                " • Zip contents of '%s' (and optionally download)" % tail,
                " • Chmod '%s'" % tail,
                " • Chown '%s'" % tail,
                " • Delete '%s' (must be empty)" % tail,
                " • Sort by filename %s" % ("descending" if self.orderBy == self.SORT_BY_NAME and not self.orderReverse else "ascending"),
                " • Sort by type (file/folder) %s" % ("- folders first" if self.orderBy == self.SORT_BY_TYPE and not self.orderReverse else "- files first"),
                " • Sort by size %s" % ("ascending" if self.orderBy == self.SORT_BY_SIZE and self.orderReverse else "descending"),
                " • Sort by last modified %s" % ("descending" if self.orderBy == self.SORT_BY_MODIFIED and not self.orderReverse else "ascending"),
                " • %s hidden files / folders" % ("Hide" if self.showHidden else "Show"),
                " • Options - Selecting opens immediately%s" % (" [SELECTED]" if self.browsingMode == "edit" else ""),
                " • Options - Selecting shows maintenance menu%s" % (" [SELECTED]" if self.browsingMode == "maintenance" else ""),
                " • %s extended file / folder info" % ("Hide" if self.fileInfo else "Display"),
                " • Refresh folder list"
            ]
            self.show_quick_panel(self.folderOptions, self.handle_folder_options)
        elif selection == 2:
            self.list_bookmarks()
        else:
            # If we're going up a folder...
            if selection == 3:
                fileType = self.FILE_TYPE_FOLDER
            else:
                # Possibly a symlink, check that first...
                f = self.get_file_from_cat(self.join_path(self.lastDir, selected))
                fileType = f["/"][self.STAT_KEY_TYPE]
            if fileType == self.FILE_TYPE_SYMLINK:
                # It is a symlink, resolve that to a file or folder:
                (path, fileType) = self.get_symlink_dest(
                    f["/"][self.STAT_KEY_DESTINATION]
                )
                if fileType is False:
                    # We don't know where it links to
                    if path[-1] != "/":
                        path += "/"
                    cmd = "ls %s %s" % (
                        self.escape_remote_path(self.lsParams),
                        self.escape_remote_path(path)
                    )
                    if not self.run_ssh_command(cmd):
                        return self.command_error(cmd)
                    # Parse the ls and add to the catalogue (but don't save the file)
                    if "Not a directory" in self.lastOut:
                        fileType = self.FILE_TYPE_FILE
                    else:
                        fileType = self.FILE_TYPE_FOLDER
                        self.cat = self.parse_ls(
                            self.cat,
                            "./:\n%s" % (self.lastOut),
                            path
                        )
                self.lastDir = path
                selected = ""
            self.debug("root: %s, selected: %s" % (self.lastDir, selected))
            if fileType == self.FILE_TYPE_FILE:
                self.maintain_or_download(selected)
            elif fileType == self.FILE_TYPE_FOLDER:
                if selection == 3:
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
            if not self.download_and_open(selected):
                return self.error_message("Error downloading %s" % selected)
        else:
            # give options
            # rename, chmod, chown, delete
            downloadFolder = os.path.expandvars(
                self.get_settings().get(
                    "download_folder",
                    "%UserProfile%\\Downloads"
                )
            )
            items = [
                [" • Edit '%s'" % selected],
                [" • Rename '%s'" % selected],
                [" • Move '%s'" % selected],
                [" • Copy '%s'" % selected],
                [" • Save to %s" % downloadFolder],
                [" • Save to %s and open" % downloadFolder],
                [" • Zip '%s'" % selected],
                [" • chmod '%s'" % selected],
                [" • chown '%s'" % selected],
                [" • Delete '%s'" % selected]
            ]
            # Show the options
            self.selected = selected
            self.show_quick_panel(items, self.handle_maintenance)

    def handle_maintenance(self, selection):
        if selection == -1:
            self.show_current_path_panel()
        elif selection == 0:
            if not self.download_and_open(self.selected):
                return sublime.error_message(
                    "Error connecting to %s" % self.serverName
                )
        elif selection == 1:
            caption = "Rename to: "
            self.show_input_panel(
                caption,
                self.selected,
                self.handle_rename,
                self.handle_change,
                self.show_list
            )
        elif selection == 2:
            #TODO
            self.error_message("TODO!")
            caption = "Move to: "
            self.show_input_panel(
                caption,
                self.selected,
                self.handle_rename,
                self.handle_change,
                self.show_list
            )
        elif selection == 3:
            #TODO
            self.error_message("TODO!")
            caption = "Copy to: "
            self.show_input_panel(
                caption,
                self.selected,
                self.handle_rename,
                self.handle_change,
                self.show_list
            )
        elif selection == 4 or selection == 5:
            # Save file to download folder
            downloadFolder = os.path.expandvars(
                self.get_settings().get(
                    "download_folder",
                    "%UserProfile%\\Downloads"
                )
            )
            if not self.download_file_to(self.selected, downloadFolder):
                return self.error_message("Error downloading %s" % self.selected)
            if selection == 5:
                # And open
                f = os.path.join(
                    downloadFolder,
                    self.selected
                )
                os.startfile(f)
        elif selection == 6:
            # items = [
            #     "bzip2",
            #     "gzip",
            #     "zip"
            # ]
            # # TODO!!
            # self.show_quick_panel(items, self.handle_compress)
            pass

        elif selection == 7:
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
            if sublime.ok_cancel_dialog(
                "Are you sure you want to delete %s?" % self.selected,
                "Delete"
            ):
                cmd = "rm %s" % (
                    self.escape_remote_path(
                        self.join_path(self.lastDir, self.selected)
                    )
                )
                if not self.run_sftp_command(cmd) or "failure" in self.lastOut:
                    return self.command_error(cmd)
                f = self.get_file_from_cat(self.lastDir)
                del f[self.selected]
                self.list_directory(self.lastDir)
            self.show_quick_panel(self.items, self.handle_list)

    def handle_rename(self, fileName):
        if self.selected is -1:
            (head, tail) = self.split_path(self.lastDir)
        else:
            head = self.lastDir
            tail = self.selected
        if tail != fileName:
            oldPath = self.join_path(head, tail)
            newPath = self.join_path(head, fileName)
            cmd = "mv %s %s" % (
                self.escape_remote_path(oldPath),
                self.escape_remote_path(newPath)
            )
            if not self.run_sftp_command(cmd):
                return self.command_error(cmd)
            else:
                if self.selected is -1:
                    self.lastDir = newPath
                f = self.get_file_from_cat(head)
                f[fileName] = f[tail]
                del f[tail]
            self.list_directory(self.lastDir)
        self.show_quick_panel(self.items, self.handle_list)

    def handle_compress(self, selection):
        if selection == -1:
            return self.show_quick_panel(self.items, self.handle_list)
        elif selection == 0:
            ext = "bz2"
        elif selection == 1:
            ext = "tar.gz"
        elif selection == 2:
            ext = "zip"
        else:
            return self.error_message("Unknown compression method")
        self.selection = selection
        fileName = "%s.%s" % (self.split_path(self.lastDir)[1], ext)
        self.items = [
            "When complete download to %s" % (
                os.path.expandvars(self.get_settings().get("download_folder"))
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
            ext = "bz2"
            cmd = "tar --bzip2 -cf"
        elif self.selection == 1:
            ext = "tar.gz"
            cmd = "tar -czf"
        elif self.selection == 2:
            ext = "zip"
            cmd = "zip -rq"
        compressTo = "%s/%s.%s" % (
            self.tempPath,
            self.split_path(self.lastDir)[1],
            ext
        )
        cmd = "cd %s && %s %s %s && echo $((66666 + 44445));" % (
            self.escape_remote_path(self.lastDir),
            cmd,
            self.escape_remote_path(compressTo),
            "."
        )
        checkReturn = "111111"
        success = self.run_ssh_command(
            cmd,
            checkReturn=checkReturn,
            listenAttempts=2
        )
        if not success:
            self.debug("Error compressing folder %s" % self.lastDir)
            self.error_message("Error compressing \"%s\"" % self.lastDir)
            self.list_directory(self.lastDir)
        else:
            self.debug("Successfully compressed folder %s" % self.lastDir)
            if download:
                downloadFolder = os.path.expandvars(
                    self.get_settings().get("download_folder")
                )
                localPath = "%s.%s" % (
                    os.path.join(
                        downloadFolder,
                        self.escape_local_path(self.split_path(self.lastDir)[1])
                    ),
                    ext
                )
                cmd = "get %s %s" % (
                    self.escape_remote_path(compressTo),
                    self.escape_remote_path(localPath)
                )
                success = self.run_sftp_command(cmd)
                if success:
                    self.debug("Successfully downloaded file %s" % localPath)
                    sublime.message_dialog("File %s.%s downloaded successfully" % (
                        os.path.join(
                            downloadFolder,
                            self.split_path(self.lastDir)[1]
                        ),
                        ext
                    ))
                    # Tidy up after ourselves
                    cmd = "del %s" % (
                        self.escape_remote_path(compressTo)
                    )
                    success = self.run_sftp_command(cmd)
                    if success:
                        self.debug("Successfully removed file %s" % compressTo)
                    else:
                        self.debug("Error deleting file %s" % compressTo)
                else:
                    self.debug("Error downloading file to %s" % localPath)
                    self.error_message("File compressed successfully but download failed. Your compressed file is at \"%s\"" % compressTo)
                self.list_directory(self.lastDir)
            else:
                cmd = "mv %s %s" % (
                    self.escape_remote_path(compressTo),
                    self.escape_remote_path("%s.%s" % (
                        self.join_path(
                            self.lastDir,
                            self.split_path(self.lastDir)[1]
                        ),
                        ext
                    ))
                )
                # For some odd reason psftp fails to move a file from /tmp to
                # /home. Permissions and everything are fine, I just get an error
                # code 4 back from sftp and the error messsage "failure". This
                # is on debian stable. Needs more investigation, psftp should be
                # fine for this simple move operation.
                success = self.run_ssh_command(cmd)
                if not success or "failure" in self.lastOut:
                    self.error_message("Error moving compressed file to %s" % (
                        self.lastDir
                    ))
                self.list_directory(self.lastDir, forceReload=True)
        self.list_directory(self.lastDir)
        self.show_quick_panel(self.items, self.handle_list)

    def handle_chmod(self, chmod):
        if self.selected is -1:
            fileName = self.lastDir
        else:
            fileName = self.join_path(self.lastDir, self.selected)
            self.debug(fileName, self.lastDir, self.selected)
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
        success = self.run_sftp_command(cmd)
        if not success:
            return self.command_error(cmd)
        else:
            f = self.get_file_from_cat(fileName)
            f["/"][self.STAT_KEY_PERMISSIONS] = int(str(chmod), 8)
            self.show_quick_panel(self.items, self.handle_list)

    def handle_chown(self, chown):
        if self.selected is -1:
            fileName = self.lastDir
        else:
            fileName = self.join_path(self.lastDir, self.selected)
        # Chown is not available in psftp
        cmd = "chown %s %s" % (
            chown,
            self.escape_remote_path(fileName)
        )
        if not self.run_ssh_command(cmd):
            return self.command_error(cmd)
        else:
            if "Operation not permitted" in self.lastOut:
                self.error_message(
                    "You do not have permission to chown \"%s\" to \"%s\"" % (
                        fileName,
                        chown
                    )
                )
            else:
                self.list_directory(self.lastDir, forceReload=True)
            self.show_quick_panel(self.items, self.handle_list)

    def show_list(self):
        self.show_quick_panel(self.items, self.handle_list)

    def get_user_and_group(self, filePath):
        user = None
        group = None
        stats = self.get_file_stats(filePath)
        if not stats:
            (head, tail) = self.split_path(filePath)
            self.list_directory(head)
            stats = self.get_file_stats(filePath)
        try:
            user = self.cat["/"]["users"][stats[self.STAT_KEY_USER]]
            group = self.cat["/"]["groups"][stats[self.STAT_KEY_GROUP]]
        except:
            user = "UNKNOWN"
            group = "UNKNOWN"
            pass
        return (user, group)

    def get_perms(self, filePath):
        stats = self.get_file_stats(filePath)
        if not stats:
            (head, tail) = self.split_path(filePath)
            self.list_directory(head)
            stats = self.get_file_stats(filePath)
        return oct(stats[self.STAT_KEY_PERMISSIONS])[2:5]

    def get_file_stats(self, filePath):
        f = self.get_file_from_cat(filePath)
        if not f:
            (head, tail) = self.split_path(filePath)
            self.list_directory(head)
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
            if self.showHidden or (not self.showHidden and f[0] != "."):
                if fileDict[f]["/"][self.STAT_KEY_TYPE] == self.FILE_TYPE_FILE:
                    self.items.append([f, self.join_path(filePath, f)])
                else:
                    self.append_files_from_path(
                        fileDict[f],
                        self.join_path(filePath, f)
                    )

    def handle_navigate(self, path):
        prevDir = self.lastDir
        self.lastDir = path
        s = self.list_directory(path)
        f = self.get_file_from_cat(path)
        if not s and not f:
            # Error message
            sublime.error_message(
                "Path \"%s\" not found" % path
            )
            self.lastDir = prevDir
            self.list_directory(path)
            return self.show_quick_panel(self.items, self.handle_list)
        # Show the options
        if f and "/" in f and f["/"][self.STAT_KEY_TYPE] == self.FILE_TYPE_FILE:
            return self.maintain_or_download(path)
        self.show_quick_panel(self.items, self.handle_list)

    def handle_folder_options(self, selection):
        if selection == 0 or selection == -1:
            # Back to prev list
            self.show_quick_panel(self.items, self.handle_list)
        elif selection == 1:
            # Disconnect from this server
            self.serverName = None
            self.run()
        elif selection == 2:
            # Fuzzy file name from here
            # TODO: ONLY SHOW THIS IF WE HAVE A CATALOGUE
            self.items = []
            self.append_files_from_path(
                self.get_file_from_cat(self.lastDir),
                self.lastDir
            )
            self.show_quick_panel(self.items, self.handle_fuzzy)
        elif selection == 3:
            # Search within files from here
            caption = "Enter search term: "
            self.show_input_panel(
                caption,
                "",
                self.handle_grep,
                self.handle_change,
                self.show_list
            )
        elif selection == 4:
            # New file
            caption = "Enter file name: "
            self.show_input_panel(
                caption,
                "",
                self.handle_new_file,
                self.handle_change,
                self.show_list
            )
        elif selection == 5:
            # New folder
            caption = "Enter folder name: "
            self.show_input_panel(
                caption,
                "",
                self.handle_new_folder,
                self.handle_change,
                self.show_list
            )
        elif selection == 6:
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
        elif selection == 7:
            # Move
            self.selected = self.lastDir.rstrip("/")
            (head, tail) = self.split_path(self.lastDir)
            self.lastDir = head
            self.moving = tail
            self.list_directory(self.lastDir, skipOptions=True, foldersOnly=True)
            self.items.insert(0, ".. Up a folder")
            self.itemPaths.insert(0, "UP")
            self.items.insert(0, "Move %s to %s/" % (self.moving, head))
            self.itemPaths.insert(0, "MOVE")
            self.show_quick_panel(self.items, self.handle_move)
        elif selection == 8:
            # copy
            # #TODO
            return self.error_message("TODO!")
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
        elif selection == 9:
            # List folder contents
            self.list_directory(self.lastDir, forceReload=True)
            ls = ""
            for line in self.lastOut.split("\n")[1:-1]:
                ls += "%s\n" % line.strip()
            self.window.run_command(
                "remote_edit_list_folder",
                {
                    "path": self.lastDir,
                    "contents": ls
                }
            )
            # Back to prev list
            self.show_quick_panel(self.items, self.handle_list)
        elif selection == 10:
            # zip
            items = [
                "bzip2",
                "gzip",
                "zip"
            ]
            # TODO: Append date to filename
            self.show_quick_panel(items, self.handle_compress)
        elif selection == 11:
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
        elif selection == 12:
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
        elif selection == 13:
            # delete
            self.selected = -1
            (head, tail) = self.split_path(self.lastDir)
            if sublime.ok_cancel_dialog(
                "Are you sure you want to delete %s?" % tail,
                "Delete"
            ):
                cmd = "rmdir %s" % self.escape_remote_path(tail)
                if not self.run_sftp_command(cmd) or "failure" in self.lastOut:
                    return self.command_error(cmd)
                self.lastDir = head
                f = self.get_file_from_cat(self.lastDir)
                del f[tail]
                self.list_directory(self.lastDir)
            self.show_quick_panel(self.items, self.handle_list)
        elif selection == 14:
            if self.orderBy == self.SORT_BY_NAME:
                self.orderReverse = False if self.orderReverse else True
            else:
                self.orderBy = self.SORT_BY_NAME
                self.orderReverse = False
            self.list_directory(self.lastDir)
            self.show_quick_panel(self.items, self.handle_list)
        elif selection == 15:
            if self.orderBy == self.SORT_BY_TYPE:
                self.orderReverse = False if self.orderReverse else True
            else:
                self.orderBy = self.SORT_BY_TYPE
                self.orderReverse = False
            self.list_directory(self.lastDir)
            self.show_quick_panel(self.items, self.handle_list)
        elif selection == 16:
            if self.orderBy == self.SORT_BY_SIZE:
                self.orderReverse = False if self.orderReverse else True
            else:
                self.orderBy = self.SORT_BY_SIZE
                self.orderReverse = True
            self.list_directory(self.lastDir)
            self.show_quick_panel(self.items, self.handle_list)
        elif selection == 17:
            if self.orderBy == self.SORT_BY_MODIFIED:
                self.orderReverse = False if self.orderReverse else True
            else:
                self.orderBy = self.SORT_BY_MODIFIED
                self.orderReverse = False
            self.list_directory(self.lastDir)
            self.show_quick_panel(self.items, self.handle_list)
        elif selection == 18:
            # Show / hide hidden files
            self.showHidden = self.showHidden is False
            self.list_directory(self.lastDir)
            self.show_quick_panel(self.items, self.handle_list)
        elif selection == 19:
            # edit mode
            self.browsingMode = "edit"
            self.list_directory(self.lastDir)
            self.show_quick_panel(self.items, self.handle_list)
        elif selection == 20:
            # maintenance mode
            self.browsingMode = "maintenance"
            self.list_directory(self.lastDir)
            self.show_quick_panel(self.items, self.handle_list)
        elif selection == 21:
            # Turn on / off extended file / folder info
            self.fileInfo = self.fileInfo is False
            self.list_directory(self.lastDir)
            self.show_quick_panel(self.items, self.handle_list)
        elif selection == 22:
            # Refresh list
            self.list_directory(self.lastDir, forceReload=True)
            self.show_quick_panel(self.items, self.handle_list)
        else:
            # we shouldn't ever hit this
            return

    def list_bookmarks(self):
        bookmarks = sublime.load_settings(self.bookmarksSettingsFile)
        serverBookmarks = bookmarks.get(self.serverName, [])
        self.items = []
        self.itemPaths = []
        if self.lastDir not in serverBookmarks:
            self.items = [
                [" • Add new bookmark at \"%s\"" % self.lastDir]
            ]
            self.itemPaths = ["ADD"]
        if len(serverBookmarks) > 0:
            self.items.append(" • Edit a bookmark")
            self.items.append(" • Delete a bookmark")
            self.itemPaths.append("EDIT")
            self.itemPaths.append("DEL")
        for b in serverBookmarks:
            self.items.append("Go to: %s" % b)
            self.itemPaths.append(b)
        self.show_quick_panel(self.items, self.handle_bookmarks_list)

    def handle_bookmarks_list(self, selection):
        if selection == -1:
            return self.show_current_path_panel()
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
        if not folderName:
            self.show_quick_panel(self.folderOptions, self.handle_folder_options)
        else:
            cmd = "mkdir %s" % (
                self.escape_remote_path(
                    self.join_path(self.lastDir, folderName)
                )
            )
            if not self.run_sftp_command(cmd):
                return self.command_error(cmd)
            self.list_directory(self.lastDir, forceReload=True)
            self.lastDir = self.join_path(self.lastDir, folderName)
        self.items = []
        self.add_options_to_items()
        self.show_quick_panel(self.items, self.handle_list)

    def handle_move(self, selection):
        if selection == -1:
            self.show_current_path_panel()
        elif selection == 0:
            # Move self.selected to self.lastDir
            cmd = "mv %s %s" % (
                self.escape_remote_path(self.selected),
                self.escape_remote_path(self.lastDir.rstrip("/") + "/")
            )
            if self.run_ssh_command(cmd):
                (head, tail) = self.split_path(self.selected)
                self.list_directory(head, forceReload=True)
                self.show_current_path_panel(forceReload=True)
            else:
                self.list_directory(self.lastDir, foldersOnly=True, skipOptions=True)
                self.items.insert(0, ".. Up a folder")
                self.itemPaths.insert(0, "UP")
                self.items.insert(0, "Move %s to %s/" % (self.moving, self.lastDir))
                self.itemPaths.insert(0, "MOVE")
                self.show_quick_panel(self.items, self.handle_move)
        else:
            if selection == 1:
                (head, tail) = self.split_path(self.lastDir)
                self.lastDir = head
            else:
                self.lastDir = self.join_path(
                    self.lastDir,
                    self.itemPaths[selection]
                )
            self.list_directory(self.lastDir, foldersOnly=True, skipOptions=True)
            self.items.insert(0, "Move %s to %s/" % (self.moving, self.lastDir))
            self.itemPaths.insert(0, "MOVE")
            self.show_quick_panel(self.items, self.handle_move)

    def add_options_to_items(self):
        if self.fileInfo:
            (head, tail) = self.split_path(self.lastDir)
            self.items.insert(0, [
                ".. Up a folder",
                "Up to %s" % head
            ])
            self.items.insert(0, [
                " • Bookmarks",
                "List all bookmarks, manage them, add new"
            ])
            self.items.insert(0, [
                " • Folder Actions / Settings [%s mode]" % self.browsingMode.capitalize(),
                "Manage folder %s or change preferences" % self.lastDir
            ])
            self.items.insert(0, ["%s:%s  " % (
                self.serverName,
                self.lastDir
            ), self.get_server_setting("host")])
        else:
            self.items.insert(0, ".. Up a folder")
            self.items.insert(0, " • Bookmarks")
            self.items.insert(0, " • Folder Actions / Settings [%s mode]" % self.browsingMode.capitalize())
            self.items.insert(0, "%s:%s" % (
                self.serverName,
                self.lastDir
            ))
        self.itemPaths.insert(0, "Nothing")
        self.itemPaths.insert(0, "to")
        self.itemPaths.insert(0, "see")
        self.itemPaths.insert(0, "here")

    def get_local_tmp_path(self, includeServer=True):
        # TODO: Make this not clash!!!
        if includeServer:
            return os.path.join(
                os.path.expandvars("%temp%"),
                "RemoteEdit",
                self.serverName
            )
        return os.path.join(
            os.path.expandvars("%temp%"),
            "RemoteEdit"
        )

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

    def download_and_open(self, f):
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
        if not self.run_sftp_command(cmd):
            return self.command_error(cmd)
        # These persist between app reloads. W00T W00T
        reData = {
            "serverName": self.serverName,
            "fileName": f,
            "path": self.lastDir,
            "openedAt": time.time()
        }
        self.window.open_file(localFile)
        self.window.active_view().settings().set("reData", reData)
        return True

    def download_file_to(self, f, destination):
        destFile = os.path.join(
            destination,
            self.escape_local_path(f)
        )
        # TODO, why the cd first?
        try:
            cd = True
            if self.psftp["pwd"] == self.lastDir:
                cd = False
        except:
            pass
        if cd:
            cmd = "cd %s" % (
                self.escape_remote_path(self.lastDir)
            )
            if not self.run_sftp_command(cmd):
                return self.error_message("Error downloading %s" % f, True)
        cmd = "get %s %s" % (
            self.escape_remote_path(f),
            self.escape_remote_path(destFile)
        )
        if not self.run_sftp_command(cmd):
            return self.error_message("Error downloading %s" % f, True)
        return True

    def list_directory(self, d, dontLoop=False, forceReload=False, foldersOnly=False, skipOptions=False):
        self.items = []
        self.itemPaths = []
        found = False
        if not forceReload and self.cat:
            # Display options based on the catalogue and self.lastDir
            try:
                self.debug("Trying cat for folder \"%s\"" % d)
                found = True
                fldr = self.get_file_from_cat(d)
                if "/NO_INDEX/" in fldr:
                    # It's in our catalogue but we haven't listed contents yet
                    raise Exception("Path not in catalogue")
                # self.debug("D is: %s" % d)
                # self.debug("Fldr is: %s" % fldr)
                for f in sorted(filter(self.remove_stats, fldr), reverse=self.orderReverse, key=lambda x: fldr[x]["/"][self.orderBy] if self.orderBy != self.SORT_BY_NAME else x.lower()):
                    # self.debug("F is: %s" % f)
                    if self.showHidden or (not self.showHidden and f[0] != "."):
                        if fldr[f]["/"][self.STAT_KEY_TYPE] == self.FILE_TYPE_FOLDER:
                            fileName = "%s/" % f
                        elif fldr[f]["/"][self.STAT_KEY_TYPE] == self.FILE_TYPE_FILE:
                            fileName = f
                            if foldersOnly:
                                continue
                        else:
                            # A symlink
                            fileName = "%s (Symlink to: %s)" % (f, fldr[f]["/"][self.STAT_KEY_DESTINATION])
                            if foldersOnly:
                                continue
                        if self.fileInfo:
                            self.items.append(
                                [
                                    fileName,
                                    "%s  %s %s %s %s" % (
                                        oct(fldr[f]["/"][self.STAT_KEY_PERMISSIONS])[2:5],
                                        self.cat["/"]["users"][fldr[f]["/"][self.STAT_KEY_USER]],
                                        self.cat["/"]["groups"][fldr[f]["/"][self.STAT_KEY_GROUP]],
                                        "" if fldr[f]["/"][self.STAT_KEY_TYPE] == self.FILE_TYPE_FOLDER else " %s " % self.display_size(fldr[f]["/"][self.STAT_KEY_SIZE]),
                                        self.display_time(fldr[f]["/"][self.STAT_KEY_MODIFIED])
                                    )
                                ]
                            )
                        else:
                            # self.debug("Key: %s, Val: %s" % (f, fldr[f]))
                            self.items.append(fileName)
                        self.itemPaths.append(f)
            except Exception as e:
                self.debug("\"%s\" not in catalogue. Exception: %s" % (d, e))
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
            cmd = "ls %s %s" % (self.lsParams, self.escape_remote_path(d))
            if not self.run_ssh_command(cmd):
                return False
            if "Not a directory" in self.lastOut:
                # Make sure we have details of the file
                (head, tail) = self.split_path(d)
                self.list_directory(
                    head,
                    dontLoop=dontLoop,
                    forceReload=forceReload,
                    foldersOnly=foldersOnly,
                    skipOptions=skipOptions
                )
                return False
            # Parse the ls and add to the catalogue (but don't save the file)
            self.cat = self.parse_ls(
                self.cat,
                "./:\n%s" % (self.lastOut),
                d
            )
            dontLoop = True
            forceReload = False
            return self.list_directory(
                d,
                dontLoop=dontLoop,
                forceReload=forceReload,
                foldersOnly=foldersOnly,
                skipOptions=skipOptions
            )
        reData = self.window.active_view().settings().get("reData", None)
        if reData and self.serverName == reData["serverName"]:
            reData["browse_path"] = self.lastDir
            self.window.active_view().settings().set("reData", reData)
        if not skipOptions:
            self.add_options_to_items()
        return True

    def show_current_path_panel(self, forceReload=False, doCat=True):
        s = self.list_directory(self.lastDir, forceReload=forceReload)
        if not s:
            # error message
            return self.error_message(
                "Error changing folder to %s" % self.lastDir
            )
        else:
            # Show the options
            self.show_quick_panel(self.items, self.handle_list)
            if doCat:
                self.check_cat()

    def remove_stats(self, f):
        return False if not f or f == "/" else True

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
                self.debug("Already cat'ing")
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
        elif os.path.exists(self.catFile):
            self.cat = pickle.load(open(self.catFile, "rb"))
            self.debug("I've reloaded!")
            self.forceReloadCat = False
        else:
            # check bgCat for X minx in past, if too long then trigger the
            # download again
            pass

    def cat_server(self, result=None, cp=None):
        # TODO set a flag for known hosts after first connect
        # if the flag is set we can punt the plink query straight into the
        # background thread without worrying about known hosts
        if cp:
            (resultServer, resultStep) = cp.split("/")
        else:
            self.bgCatStep = 0
            resultServer = self.serverName
            resultStep = 0
        self.debug("Cat server called for %s, step is %s" % (
            resultServer,
            self.bgCatStep
        ))

        # TODO: Replace magic numbers
        # Put in some retrys
        # Make individual bits callable manually (refresh from... etc)

        if resultServer != self.serverName:
            return self.tidy_cat_process()

        # STEP 1
        if self.bgCatStep is 0:
            # TODO, Much better error messages
            self.bgCatStep += 1
            passthrough = "%s/%s" % (self.serverName, self.bgCatStep)

            self.catFile = os.path.join(
                self.get_cat_path(),
                "%s.cat" % self.serverName
            )
            self.localCatFolder = self.get_local_tmp_path()
            self.catFileName = "%sSub.tar.gz" % self.serverName
            self.remoteCatGzPath = "%s/%s" % (self.tempPath, self.catFileName)
            self.localCatGzPath = os.path.join(
                self.localCatFolder,
                self.catFileName
            )

            try:
                os.makedirs(self.localCatFolder)
            except FileExistsError:
                # Directory already exists
                pass
            except Exception as e:
                self.debug("Exception when making local folder: %s" % e)
                return False

            # Dont blindly connect here even though we send 1
            # TODO: If this is found we should just inform the user that they
            # need to connect manually (or set a config flag to add it???)
            # if "host key is not cached in the registry" in self.lastErr:
            #     send = "n\n"
            #     plink["process"].stdin.write(bytes(send, "utf-8"))
            #     self.await_response(plink)

            # We should be at a prompt
            cmd = "cd %s && ls %s -R > %s/%sSub.cat 2>/dev/null; cd %s && rm %sSub.tar.gz; tar cfz %sSub.tar.gz %sSub.cat && rm %sSub.cat && echo $((666 + 445));\n" % (
                self.escape_remote_path(self.get_server_setting("cat_path")),
                self.lsParams,
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
                checkReturn="1111",
                listenAttempts=10,
                callback=self.cat_server,
                callbackPassthrough=passthrough
            )
        elif self.bgCatStep is 1:
            if resultStep is not "1":
                return self.tidy_cat_process()
            if not result["success"]:
                return self.tidy_cat_process()
            self.bgCatStep += 1
            passthrough = "%s/%s" % (self.serverName, self.bgCatStep)

            # Now grab the file
            cmd = "get %s %s" % (
                self.escape_remote_path(self.remoteCatGzPath),
                self.escape_remote_path(self.localCatGzPath)
            )
            self.run_sftp_command(
                cmd,
                callback=self.cat_server,
                callbackPassthrough=passthrough
            )

        elif self.bgCatStep is 2:
            if resultStep is not "2":
                return self.tidy_cat_process()
            if not result["success"]:
                return self.tidy_cat_process()
            self.bgCatStep += 1
            passthrough = "%s/%s" % (self.serverName, self.bgCatStep)

            # delete tmp file from server
            cmd = "del %s" % (
                self.escape_remote_path(self.remoteCatGzPath)
            )
            self.run_sftp_command(
                cmd,
                callback=self.cat_server,
                callbackPassthrough=passthrough
            )
        elif self.bgCatStep is 3:
            # split this one up more
            if resultStep is not "3":
                return self.tidy_cat_process()
            if not result["success"]:
                return self.tidy_cat_process()

            # Check local file exists
            try:
                f = tarfile.open(self.localCatGzPath, "r:gz")
                f.extractall(self.localCatFolder)
                f.close()
            except Exception as e:
                self.debug("Gzip fail: \"%s\"" % e)
                return False
            catDataFile = os.path.join(
                self.localCatFolder,
                "%sSub.cat" % self.serverName
            )
            cat = self.create_cat(
                catDataFile,
                self.get_server_setting("cat_path")
            )
            # Delete the local files we downloaded and untarred
            os.remove(self.localCatGzPath)
            os.remove(catDataFile)
            # Save our python dict catalogue by pickleing it in some tangy,
            # slightly sweet, pickling vinegar.
            f = open(self.catFile, "wb")
            # Pickled egg?
            pickle.dump(cat, f)
            # Yes please! Don't mind if I do.
            f.close()
            self.debug("Catalogued. :)")
            if len(self.sftpThreads) > 1:
                self.debug("Len of SFTP is %s" % len(self.sftpThreads))
                self.remove_sftp_thread()
            if len(self.sshThreads) > 1:
                self.debug("Len of SSH is %s" % len(self.sshThreads))
                self.remove_ssh_thread()
        self.tidy_cat_process()

    def tidy_cat_process(self, resultServer=None):
        # Remove the extra threads
        # TODO: Cleanup local files
        # reset all variables
        # retry's here
        pass

    def create_cat(self, fileName, startAt):
        # Build our catalogue dictionary from one big recursive ls of the root
        # folder. The structure of the dict will be something like:
        #
        # cat["/"]["server"] = server name
        # cat["/"]["created"] = unixtime created
        # cat["/"]["updated"] = unixtime updated
        # cat["/"]["users"] = users dict int -> user name
        # cat["/"]["groups"] = groups dict int -> group name
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
        # [6] - if symlink then note where it links to
        cat = {}
        catFile = open(fileName, "r", encoding="utf-8", errors="ignore")
        cat = self.parse_ls(cat, catFile.read(), startAt)
        catFile.close()
        return cat

    def parse_ls(self, cat, lsData, startAt, users=[], groups=[]):
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
        if "/" in cat and "users" in cat["/"]:
            users = cat["/"]["users"]
            groups = cat["/"]["groups"]
        tmpCat = cat
        tmpStartCat = cat
        for f in filter(bool, startAt.split('/')):
            if f not in tmpStartCat:
                tmpStartCat[f] = {}
            tmpStartCat = tmpStartCat[f]
        f_f_fresh = False
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
                        tmpCat[f] = {}
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
            elif self.lsParams in line:
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
                if name == "." and len(options) is 0:
                    charsIn1 = line.find("./")
                # Verify that with the ".." up a dir
                elif name == ".." and len(options) is 0:
                    charsIn2 = line.find("../")
                elif len(sl) < 5:
                    # Skip the "Total BYTES" message
                    pass
                elif not charsIn1 or not charsIn2 or charsIn1 != charsIn2:
                    self.debug("Error parsing ls output at line: \"%s\"" % line)
                else:
                    cName = line[charsIn1:].strip()
                    if sl[0][0] == "l" and "->" in cName:
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
                        self.debug(
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
                            self.debug(
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
                            self.debug("Error parsing file size in line: %s" % line)
                            s = 0
                    try:
                        d = int(time.mktime(time.strptime(
                            "%s %s" % (sl[5], sl[6]),
                            "%Y-%m-%d %H:%M"
                        )))
                    except:
                        self.debug("Can't parse date at line: \"%s\"" % line)
                        continue
                    stats = [t, p, u, g, s, d]
                    # If we have a symlink
                    if t is self.FILE_TYPE_SYMLINK:
                        stats.append(symlinkDest)
                    # self.debug("%s: %s" % (cName, str(stats))
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
                tmpCat[f] = {}
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
        if "/" not in cat:
            cat["/"] = {}
        cat["/"]["server"] = self.serverName
        if "created" not in cat["/"]:
            cat["/"]["created"] = int(time.time())
        cat["/"]["updated"] = int(time.time())
        cat["/"]["users"] = users
        cat["/"]["groups"] = groups
        return cat

    def up_dir_to_path(self, symlinkDest, prepend):
        # self.debug("To: %s, path: %s" % (symlinkDest, prepend))
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
        elif setting == "type":
            value = self.SORT_BY_TYPE
        elif setting == "size":
            value = self.SORT_BY_SIZE
        elif setting == "modified":
            value = self.SORT_BY_MODIFIED
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
            self.debug(self.lastJsonifyError)
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

    def load_server_list(self):
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

    def command_error(self, cmd):
        sublime.error_message(
            "Command \"%s\" failed on %s" % (
                cmd,
                self.serverName
            )
        )
        return False

    def error_message(self, msg, useLastError=False):
        if useLastError and self.lastErr:
            return sublime.error_message(self.lastErr)
        sublime.error_message(msg)
        return False

    def debug(self, data):
        if len(data) > 3000:
            print("MAIN %s: %s" % (time.strftime("%H:%M:%S"), data[0:3000]))
        else:
            print("MAIN %s: %s" % (time.strftime("%H:%M:%S"), data))

    def run_ssh_command(
        self,
        cmd,
        checkReturn=None,
        listenAttempts=1,
        timeout=30,
        callback=None,
        callbackPassthrough=None,
        dropResults=False
    ):
        return self.run_remote_command(
            "ssh",
            cmd,
            checkReturn,
            listenAttempts,
            timeout,
            callback,
            callbackPassthrough,
            dropResults
        )

    def run_sftp_command(
        self,
        cmd,
        checkReturn=None,
        listenAttempts=1,
        timeout=30,
        callback=None,
        callbackPassthrough=None,
        dropResults=False
    ):
        return self.run_remote_command(
            "sftp",
            cmd,
            checkReturn,
            listenAttempts,
            timeout,
            callback,
            callbackPassthrough,
            dropResults
        )

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

    def run_remote_command(
        self,
        appType,
        cmd,
        checkReturn,
        listenAttempts=1,
        timeout=30,
        callback=None,
        callbackPassthrough=None,
        dropResults=False
    ):
        self.debug("run_remote_command called for %s and cmd: \"%s\"" % (
            appType,
            cmd
        ))
        if timeout is None:
            timeout = self.timeout
        work = {}
        work["server_name"] = self.serverName
        work["settings"] = self.server["settings"]
        work["cmd"] = cmd
        work["prompt_contains"] = checkReturn
        work["listen_attempts"] = listenAttempts
        work["drop_results"] = dropResults
        # Generate a unique key to listen for results on
        m = hashlib.md5()
        m.update(("%s%s" % (cmd, str(time.time()))).encode('utf-8'))
        key = m.hexdigest()
        work["key"] = key
        self.debug("....going on the queue.....")
        if appType == "sftp":
            self.sftpQueue.put(work)
        else:
            self.sshQueue.put(work)
        self.debug("....now on the queue.....")
        startTime = time.time()
        if callback:
            self.debug("Using set_timeout to call the callback handler to check for results")
            # TODO: This should be totally events driven, have a thread block
            # on a queue and on return of data call a callback. Once we've
            # moved at least a bit towards that from where we are now it should
            # be a much easier task. For now we'll just have to check the
            # results dict regularly with set timeouts.
            sublime.set_timeout(
                lambda: self.handle_callbacks(
                    key,
                    startTime + timeout,
                    callback,
                    callbackPassthrough
                ),
                100
            )
            return
        elif dropResults:
            return
        self.lastErr = self.lastOut = ""
        self.debug("....when time becomes a loop.....")
        while True:
            if startTime + timeout < time.time():
                self.debug("Timeout")
                return False
            if key in self.appResults:
                result = self.appResults[key]
                del self.appResults[key]
                self.debug("Result found for cmd: %s" % cmd)
                break
            else:
                time.sleep(0.1)
        if not callback:
            self.debug("Setting out and error return values")
            self.lastOut = result["out"]
            self.lastErr = result["err"]
            return result["success"]

    def handle_callbacks(self, key, expireTime, callback, callbackPassthrough):
        if key in self.appResults:
            self.debug("Results found in callback handler, firing the callback")
            results = self.appResults[key]
            del self.appResults[key]
            if callbackPassthrough is None:
                callback(results)
            else:
                callback(results, callbackPassthrough)
        elif time.time() > expireTime:
            callback(
                {"success": False, "out": "", "err": ""},
                callbackPassthrough
            )
        else:
            sublime.set_timeout(
                lambda: self.handle_callbacks(
                    key,
                    expireTime,
                    callback,
                    callbackPassthrough
                ),
                100
            )
