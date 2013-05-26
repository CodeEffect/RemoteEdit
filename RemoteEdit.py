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
import threading
import queue
import tarfile


class RemoteEditCommand(sublime_plugin.WindowCommand):

    servers = {}
    serverName = None
    binPath = None
    settings = None
    settingFile = "RemoteEdit.sublime-settings"
    catFile = False
    cat = None
    forceReloadCat = True
    lastDir = None
    mode = "edit"
    info = False
    showHidden = False
    dontEditExt = []
    catExcludeFolders = []
    psftp = False
    plink = False
    bgCat = False
    permsLookup = None
    lsParams = "-lap --time-style=long-iso --color=never"

    # Add links to find command results
    # Make sure temp name is unique
    # ESCAPING ESCAPING ESCAPING
    #
    # order by filename, size, date
    # per server filename filters, add filters
    # filter by file size
    # filter by date modified
    #
    # Per server config settings:
    # show / hide hidden files
    # " : File options - Selecting opens immediately%s" % (" [SELECTED]" if self.mode == "edit" else ""),
    # " : File options - Selecting shows maintenance menu%s" % (" [SELECTED]" if self.mode == "maintenance" else ""),
    # " : Turn %s extended file / folder info" % ("off" if self.info else "on")
    #
    # BOOKMARKS!
    #
    # TODO: Move catalogue into coo. test against mic

    def run(self, save=None):
        # Ensure that the self.servers dict is populated
        servers = self.load_server_list()
        if save:
            # Save called from external RE events handler class
            self.save(save)
        elif self.serverName:
            # Fire up the self.serverName server
            self.start_server(self.serverName)
        else:
            # List servers and startup options
            self.items = servers
            items = [[
                "%s (%s)" % (name, self.servers[name]["settings"]["host"]),
                "User: %s, Path: %s" % (
                    self.servers[name]["settings"]["user"],
                    self.servers[name]["settings"]["remote_path"]
                )
            ] for name in self.servers]
            items.insert(0, [
                " • Quick connect",
                "Just enter a host and a username / password"
            ])
            items.insert(0, [
                " • Add a new server",
                "Complete new server details to quickly connect in future"
            ])
            self.show_quick_panel(items, self.handle_server_select)

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
            print("File is being saved already")
            return
        # TODO: Set a status bar in progress symbol
        remoteFile = self.join_path(
            reData["path"],
            reData["fileName"]
        )
        serverName = reData["serverName"]
        if self.serverName != serverName:
            try:
                self.close_apps()
                self.server = self.servers[serverName]
            except:
                self.release_lock(lockFile)
                return self.error_message(
                    "Missing connection details for server \"%s\"" % serverName
                )
        # Initiate the save
        cmd = "put %s %s" % (
            localFile,
            remoteFile
        )
        self.psftp = self.run_sftp_command(self.psftp, cmd)
        if not self.sftpResult:
            # TODO! ALL FAILS SHOULD MARK VIEW AS DIRTY
            self.release_lock(lockFile)
            return self.command_error(cmd)
        # if succeeded then display ok
        if "permission denied" in self.lastOut:
            # TODO! ALL FAILS SHOULD MARK VIEW AS DIRTY
            self.release_lock(lockFile)
            return self.error_message(
                "Permission denied when attempting to write file to %s" % serverName
            )
        (path, fileName) = self.split_path(remoteFile)
        reData["remote_save"] = time.time()
        v.settings().set("reData", reData)
        sublime.message_dialog("File %s saved successfully" % fileName)
        self.release_lock(lockFile)

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
            saveTo = os.path.join(
                sublime.packages_path(),
                "User",
                "RemoteEdit",
                "Servers"
            )
            snippet = sublime.load_resource(
                "Packages/RemoteEdit/RENewServer.default-config"
            )
            newSrv = self.window.new_file()
            newSrv.set_name("NewServer.sublime-settings")
            newSrv.set_syntax_file("Packages/JavaScript/JSON.tmLanguage")
            self.insert_snippet(snippet)
            newSrv.settings().set("default_dir", saveTo)
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
            self.start_server(self.items[selection - 2])

    def insert_snippet(self, snippet):
        view = self.window.active_view()
        if view.is_loading():
            sublime.set_timeout(lambda: self.insert_snippet(snippet), 100)
        else:
            view.run_command("insert_snippet", {'contents': snippet})

    def start_server(self, serverName, quickConnect=False):
        try:
            if self.serverName != serverName:
                self.serverName = serverName
                self.forceReloadCat = True
                self.lastDir = self.get_server_setting("remote_path", None)
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
        except:
            self.serverName = None
            self.run()
            return

        # K, fire up a thread to pull down an ls and process it
        # meanwhile open a connection to the server and present the user with a
        # filelist etc
        self.check_cat()

        # list files
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
        s = self.list_directory(self.lastDir)
        if not s:
            # error message
            return self.error_message(
                "Error connecting to %s" % self.serverName,
                True
            )
        # Show the options
        self.show_quick_panel(self.items, self.handle_list)

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

    def close_apps(self):
        try:
            self.psftp["process"].terminate()
        except:
            pass
        try:
            self.plink["process"].terminate()
        except:
            pass

    def handle_fuzzy(self, selection):
        if selection == -1:
            self.close_apps()
            return
        (self.lastDir, selected) = self.split_path(self.items[selection][1])
        self.maintain_or_download(selected)

    def handle_grep(self, search):
        # print(search)
        if not search:
            return self.show_quick_panel(self.items, self.handle_list)
        # TODO: Better random FN, hash a view?
        # Check unused? Check TMP PATH?
        tmpFileName = "RemoteEdit_%s_grep" % time.time()
        remotePath = "/tmp/%s" % tmpFileName
        localPath = os.path.join(
            self.get_local_tmp_path(),
            tmpFileName
        )
        wCmd = self.get_command("plink.exe")
        self.plink = self.get_process(wCmd)
        self.await_response(self.plink)
        if self.plink["process"].poll() is not None:
            print("Error connecting to server %s" % self.serverName)
            return False
        # We should be at a prompt
        exclude = ""
        if self.catExcludeFolders:
            for f in self.catExcludeFolders:
                exclude += "--exclude-dir=\"%s\" " % f
        # TODO: Consider gzipping these as per the ls
        cmd = "cd %s && grep -i %s-nR -A2 -B2 \"%s\" . > %s 2>/dev/null; echo %s;" % (
            self.lastDir,
            exclude,
            search,
            remotePath,
            "\"GREPPING\" $((66666 + 44445)) \"GREPGREPGREPGREPGREPALOT\""
        )
        checkReturn = "111111"
        if not self.run_ssh_command(self.plink, cmd, checkReturn, 2):
            return self.command_error(cmd)
        # Download results
        cmd = "get %s %s" % (
            remotePath,
            localPath
        )
        if not self.run_sftp_command(self.psftp, cmd):
            return self.command_error(cmd)
        try:
            lf = open(localPath, "r", encoding="utf-8", errors="ignore")
            results = lf.read()
            lf.close()
        except:
            # TODO, return here && error msg
            results = ""
        # Parse the results
        i = 0
        matches = 0
        files = {}
        inResult = False
        resultsText = []
        resultsText.append("Searching for \"%s\" on %s\n" % (
            search,
            self.serverName
        ))
        aroundLine = re.compile("\.\/(.+)-([0-9]+)-(.*)")
        resultLine = re.compile("\.\/(.+):([0-9]+):(.*)")
        for line in results.split("\n"):
            i += 1
            if i is 1:
                # First line is our search command
                continue
            if "GREPPING 111111 GREPGREPGREPGREPGREPALOT" in line:
                # We're done
                break
            # print(line[0:5])
            if line and line[0:2] == "--":
                inResult = False
                continue
            aroundMatch = re.search(aroundLine, line)
            resultMatch = re.search(resultLine, line)
            if aroundMatch:
                fileName = aroundMatch.group(1)
            elif resultMatch:
                fileName = resultMatch.group(1)
            if not inResult and (resultMatch or aroundMatch):
                inResult = True
                if aroundMatch.group(1) in files:
                    resultsText.append("  ..\n")
                else:
                    resultsText.append("\n%s%s:\n" % (self.lastDir, fileName))
                files[aroundMatch.group(1)] = True
            if aroundMatch:
                resultsText.append("  %s%s\n" % (
                    aroundMatch.group(2).ljust(4),
                    aroundMatch.group(3).rstrip()
                ))
            if resultMatch:
                matches += 1
                ln = "%s:" % resultMatch.group(2)
                resultsText.append("  %s%s\n" % (
                    ln.ljust(4),
                    resultMatch.group(3).rstrip()
                ))
        resultsText.append("\n%s matche%s across %s file%s\n\n" % (
            matches,
            "" if matches is 1 else "s",
            len(files),
            "" if len(files) is 1 else "s"
        ))
        # Open a new tab
        # TODO: Make this pass the filename instead of the text
        self.window.run_command(
            "remote_edit_display_search",
            {
                "findResults": "".join(resultsText),
                "serverName": self.serverName
            }
        )
        # Now delete remote and local files
        cmd = "del \"%s\"" % (
            remotePath
        )
        self.run_sftp_command(self.psftp, cmd)
        try:
            os.remove(localPath)
        except:
            pass

    def handle_list(self, selection):
        if selection == -1:
            self.close_apps()
            return
        if self.info:
            selected = self.items[selection][0]
        else:
            selected = self.items[selection]
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
                " • Fuzzy file name search in '%s'" % tail,
                " • Search inside files in '%s'" % tail,
                " • Create a new file within '%s'" % tail,
                " • Create a new folder within '%s'" % tail,
                " • Rename folder '%s'" % tail,
                " • Move folder '%s'" % tail,
                " • Copy folder '%s'" % tail,
                " • Zip contents of '%s' (and optionally download)" % tail,
                " • Chmod '%s'" % tail,
                " • Chown '%s'" % tail,
                " • Delete '%s' (must be empty)" % tail,
                " • %s hidden files / folders" % ("Hide" if self.showHidden else "Show"),
                " • Options - Selecting opens immediately%s" % (" [SELECTED]" if self.mode == "edit" else ""),
                " • Options - Selecting shows maintenance menu%s" % (" [SELECTED]" if self.mode == "maintenance" else ""),
                " • %s extended file / folder info" % ("Hide" if self.info else "Display"),
                " • Disconnect from server '%s'" % tail,
            ]
            self.show_quick_panel(self.folderOptions, self.handle_folder_options)
        elif selection == 2 or selected[-1] == "/":
            # Up a folder
            if selection == 2:
                if len(self.lastDir) <= 1:
                    self.lastDir = "/"
                else:
                    (head, tail) = self.split_path(self.lastDir)
                    if len(head) is 1:
                        self.lastDir = "/"
                    else:
                        self.lastDir = "%s/" % head
            else:
                self.lastDir = self.join_path(
                    self.lastDir,
                    selected
                )
            s = self.list_directory(self.lastDir)
            if not s:
                # error message
                return self.error_message(
                    "Error changing folder to %s" % self.lastDir
                )
            else:
                # Show the options
                self.show_quick_panel(self.items, self.handle_list)
                self.check_cat()
        else:
            self.maintain_or_download(selected)

    def maintain_or_download(self, selected):
        ext = selected.split(".")[-1]
        if self.mode == "edit" and ext not in self.dontEditExt:
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
        if selection == 0:
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
            #TODO
            caption = "Zip: "
            self.show_input_panel(
                caption,
                self.selected,
                self.handle_rename,
                self.handle_change,
                self.show_list
            )
        elif selection == 7:
            caption = "chmod to: "
            perms = self.get_perms(self.selected)
            self.show_input_panel(
                caption,
                perms,
                self.handle_chmod,
                self.handle_change,
                self.show_list
            )
        elif selection == 8:
            caption = "chown to: "
            (user, group) = self.get_user_and_group(self.selected)
            self.show_input_panel(
                caption,
                "%s:%s" % (user, group),
                self.handle_chown,
                self.handle_change,
                self.show_list
            )
        elif selection == 9:
            if self.ok_cancel_dialog(
                "Are you sure you want to delete %s" % self.selected,
                "Delete"
            ):
                # TODO: DELETE FILE
                pass

    def handle_rename(self, fileName):
        if self.selected is -1:
            (head, tail) = self.split_path(self.lastDir)
            cmd = "cd %s" % head
            self.psftp = self.run_sftp_command(self.psftp, cmd)
            if not self.sftpResult:
                return self.command_error(cmd)
        else:
            head = self.lastDir
            tail = self.selected
        if tail != fileName:
            cmd = "mv %s %s" % (tail, fileName)
            self.psftp = self.run_sftp_command(self.psftp, cmd)
            if not self.sftpResult:
                return self.command_error(cmd)
            else:
                # TODO: UPDATE LOCAL!!!!!!!!!!!!!!!!!
                # PATHS WILL NOT BE CORRECT
                if self.selected is -1:
                    self.lastDir = self.join_path(head, fileName)
        self.show_quick_panel(self.items, self.handle_list)

    def handle_chmod(self, chmod):
        # TODO: VALIDATE CHMOD
        if self.selected is -1:
            fileName = self.lastDir
        else:
            fileName = self.selected
        cmd = "chmod %s %s" % (chmod, fileName)
        self.psftp = self.run_sftp_command(self.psftp, cmd)
        if not self.sftpResult:
            return self.command_error(cmd)
        else:
            self.show_quick_panel(self.items, self.handle_list)

    def handle_chown(self, chown):
        # TODO: VALIDATE CHOWN
        if self.selected is -1:
            fileName = self.lastDir
        else:
            fileName = self.selected
        # TODO: CHOWN DOESN'T RUN FROM SFTP!!!!
        # UPDATE LOCAL!!!!!!!!!!!!!
        cmd = "chown %s %s" % (chown, fileName)
        self.psftp = self.run_sftp_command(self.psftp, cmd)
        if not self.sftpResult:
            return self.command_error(cmd)
        else:
            self.show_quick_panel(self.items, self.handle_list)

    def show_list(self):
        self.show_quick_panel(self.items, self.handle_list)

    def get_user_and_group(self, fileName):
        user = None
        group = None
        stats = self.get_file_stats(self.join_path(self.lastDir, fileName))
        try:
            user = self.cat["/"]["users"][stats[2]]
            group = self.cat["/"]["group"][stats[3]]
        except:
            #TODO connect in to the server and get them
            pass
        return (user, group)

    def get_perms(self, fileName):
        stats = self.get_file_stats(self.join_path(self.lastDir, fileName))
        if stats:
            return oct(stats[1])[2:5]
        #TODO connect in to the server and get them
        permsStr = None
        perms = ""
        tmp = i = 0
        for p in permsStr[1:]:
            if p is not "-":
                tmp += max(4 - (2 * i), 1)
            i += 1
            if i is 3:
                perms += str(tmp)
                i = tmp = 0
        return perms

    def get_file_stats(self, filePath):
        f = self.get_file_from_cat(filePath)
        if not f:
            # Connect to server and get info
            pass
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
        for f in fileDict:
            if f != "/" and (self.showHidden or (not self.showHidden and f[0] != ".")):
                if fileDict[f]["/"][0] == 0:
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
        if not s:
            self.lastDir = prevDir
            # error message
            sublime.error_message(
                "Path \"%s\" not found" % path
            )
        # Show the options
        self.show_quick_panel(self.items, self.handle_list)

    def handle_folder_options(self, selection):
        print(selection)
        if selection == -1:
            self.close_apps()
            return
        elif selection == 0:
            # Back to prev list
            self.show_quick_panel(self.items, self.handle_list)
        elif selection == 1:
            # Fuzzy file name from here
            # TODO: ONLY SHOW THIS IF WE HAVE A CATALOGUE
            self.items = []
            self.append_files_from_path(
                self.get_file_from_cat(self.lastDir),
                self.lastDir
            )
            self.show_quick_panel(self.items, self.handle_fuzzy)
        elif selection == 2:
            # Search within files from here
            caption = "Enter search term"
            print("SHOW GREO")
            self.show_input_panel(
                caption,
                "",
                self.handle_grep,
                self.handle_change,
                self.show_list
            )
        elif selection == 3:
            # new file
            caption = "Enter file name: "
            self.show_input_panel(
                caption,
                "",
                self.handle_new_file,
                self.handle_change,
                self.show_list
            )
        elif selection == 4:
            # new folder
            caption = "Enter folder name: "
            self.show_input_panel(
                caption,
                "",
                self.handle_new_folder,
                self.handle_change,
                self.show_list
            )
        elif selection == 5:
            # rename
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
        elif selection == 6:
            # move
            # TODO: Select new path with quick panel
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
            # copy
            # #TODO
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
        elif selection == 8:
            # zip
            # TODO
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
            # chmod
            self.selected = -1
            caption = "chmod to: "
            perms = self.get_perms(self.selected)
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
            (user, group) = self.get_user_and_group(self.selected)
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
            if self.ok_cancel_dialog(
                "Are you sure you want to delete %s" % tail,
                "Delete"
            ):
                # TODO: DELETE FILE
                pass
        elif selection == 12:
            # Show / hide hidden files
            self.showHidden = self.showHidden is False
            self.list_directory(self.lastDir)
            self.show_quick_panel(self.items, self.handle_list)
        elif selection == 13:
            # edit mode
            self.mode = "edit"
            self.list_directory(self.lastDir)
            self.show_quick_panel(self.items, self.handle_list)
        elif selection == 14:
            # maintenance mode
            self.mode = "maintenance"
            self.list_directory(self.lastDir)
            self.show_quick_panel(self.items, self.handle_list)
        elif selection == 15:
            # Turn on / off extended file / folder info
            self.info = self.info is False
            self.list_directory(self.lastDir)
            self.show_quick_panel(self.items, self.handle_list)
        elif selection == 16:
            # Disconnect from this server
            self.serverName = None
            self.close_apps()
            self.run()
        else:
            # we shouldn't ever get here
            return

    def handle_new_file(self, fileName):
        if not fileName:
            self.show_quick_panel(self.folderOptions, self.handle_folder_options)
        else:
            # make local folder
            localFolder = self.make_local_folder()
            if not localFolder:
                # error message
                return sublime.error_message(
                    "Error creating local folder"
                )
            else:
                # TODO: MAKE LOCAL FILE, SET SFTP FLAGS
                # OPEN IN EDITOR
                pass

    def handle_new_folder(self, folderName):
        if not folderName:
            self.show_quick_panel(self.folderOptions, self.handle_folder_options)
        else:
            cmd = "mkdir %s" % folderName
            self.psftp = self.run_sftp_command(self.psftp, cmd)
            if not self.sftpResult:
                return self.command_error(cmd)
            self.lastDir = self.join_path(self.lastDir, folderName)
            # cmd = "cd %s" % self.lastDir
            # if not self.run_sftp_command(self.psftp, cmd):
            #     return self.command_error(cmd)
        self.items = []
        self.add_options_to_items()
        self.show_quick_panel(self.items, self.handle_list)

    def add_options_to_items(self):
        if self.info:
            (head, tail) = self.split_path(self.lastDir)
            self.items.insert(0, [
                ".. Up a folder",
                "Up to %s" % head
            ])
            self.items.insert(0, [
                " • Folder Actions / Settings [%s mode]" % self.mode.capitalize(),
                "Manage folder %s or change preferences" % self.lastDir
            ])
            self.items.insert(0, ["%s:%s  " % (
                self.serverName,
                self.lastDir
            ), self.get_server_setting("host")])
        else:
            self.items.insert(0, ".. Up a folder")
            self.items.insert(0, " • Folder Actions / Settings [%s mode]" % self.mode.capitalize())
            self.items.insert(0, "%s:%s" % (
                self.serverName,
                self.lastDir
            ))

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
        # TODO ESCAPE FILENAMES!!!!!!!!!!!!!!
        remoteFile = self.join_path(self.lastDir, f)
        localFile = os.path.join(localFolder, f)
        cmd = "get %s %s" % (remoteFile, localFile)
        self.psftp = self.run_sftp_command(self.psftp, cmd)
        if not self.sftpResult:
            return self.command_error(cmd)

        # THESE PERSIST BETWEEN APP RELOADS. W00T W00T
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
            f
        )
        try:
            cd = True
            if self.psftp["pwd"] == self.lastDir:
                cd = False
        except:
            pass
        if cd:
            cmd = "cd %s" % self.lastDir
            self.psftp = self.run_sftp_command(self.psftp, cmd)
            if not self.sftpResult:
                return self.error_message("Error downloading %s" % f, True)
        cmd = "get %s %s" % (f, destFile)
        self.psftp = self.run_sftp_command(self.psftp, cmd)
        if not self.sftpResult:
            return self.error_message("Error downloading %s" % f, True)
        return True

    def list_directory(self, d):
        self.items = []
        if self.cat:
            # Display options based on the catalogue and self.lastDir
            try:
                fldr = self.get_file_from_cat(d)
                # print(d)
                if self.info:
                    for f in filter(bool, fldr):
                        # print(f)
                        if f != "/" and (self.showHidden or (not self.showHidden and f[0] != ".")):
                            self.items.append(
                                [
                                    "%s%s" % (f, "/" if fldr[f]["/"][0] == 1 else ""),
                                    "%s  %s %s %s %s" % (
                                        oct(fldr[f]["/"][1])[2:5],
                                        self.cat["/"]["users"][fldr[f]["/"][2]],
                                        self.cat["/"]["groups"][fldr[f]["/"][3]],
                                        "" if fldr[f]["/"][0] == 1 else " " + self.display_size(fldr[f]["/"][4]) + " ",
                                        self.display_time(fldr[f]["/"][5])
                                    )
                                ]
                            )
                else:
                    for f in filter(bool, fldr):
                        if f != "/" and (self.showHidden or (not self.showHidden and f[0] != ".")):
                            # print(f, fldr[f])
                            self.items.append(
                                "%s%s" % (
                                    f,
                                    "/" if fldr[f]["/"][0] == 1 else ""
                                )
                            )
            except Exception as e:
                self.items = []
                print("%s NOT IN CATALOGUE: %s" % (d, e))
        if not self.items:
            cmd = "ls %s %s" % (self.lsParams, d)
            self.plink = self.run_ssh_command(self.plink, cmd)
            if not self.plink:
                return self.command_error(cmd)
            # Parse th ls and add to the catalogue (but don't save)
            if self.cat:
                self.cat = self.parse_ls(
                    self.cat,
                    "./:\n%s" % (self.lastOut),
                    self.lastDir
                )
            for line in self.lastOut.split("\n"):
                la = line.split()
                f = la[-1].strip().rstrip("/")
                if len(la) > 5 and f not in [".", ".."]:
                    # TODO: Only add d, - and f
                    if la[0][0] == "d":
                        if self.showHidden or (not self.showHidden and f[0] != "."):
                            if self.info:
                                #TODO: Add the extra required info here (as above)
                                self.items.append([f + "/", "Folder"])
                            else:
                                self.items.append(f + "/")
                    else:
                        if self.showHidden or (not self.showHidden and f[0] != "."):
                            if self.info:
                                #TODO: Add the extra required info here (as above)
                                self.items.append([f, "File"])
                            else:
                                self.items.append(f)
        reData = self.window.active_view().settings().get("reData", None)
        if reData and self.serverName == reData["serverName"]:
            reData["browse_path"] = self.lastDir
            self.window.active_view().settings().set("reData", reData)
        self.add_options_to_items()
        return True

    def display_time(self, uTime):
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(uTime))

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
        # First see if we're already cataloguing
        self.bgCat = time.time()

        # First, see if we've already got a catalogue
        catPath = os.path.join(
            sublime.packages_path(),
            "User",
            "RemoteEdit",
            "Cats"
        )
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
        # And it's recent (less than 1 day old)
        if mTime + stale < time.time():
            # needs a refresh
            try:
                os.remove(self.catFile)
            except:
                pass
            # Use the below flag to indicate we will be cataloguing in the BG
            self.bgCat = time.time()
            # AWAKEN THE CAT DEMON!
            cat = threading.Thread(
                target=self.cat_server, args=(
                    self.serverName,
                    self.serverName
                )
            )
            cat.daemon = True
            cat.start()
        elif os.path.exists(self.catFile):
            self.cat = pickle.load(open(self.catFile, "rb"))
            print("I've reloaded!")
            self.forceReloadCat = False
        else:
            # check bgCat for X minx in past, if too long then trigger the
            # download again
            pass

    def cat_server(self, serverName, threadingSendsLotsOfArgsUnlessUHave2):
        # todo, set a flag for known hosts after first connect
        # if the flag is set we can punt the plink query straight into the
        # background thread without worrying about known hosts
        self.serverName = serverName
        self.catFile = os.path.join(
            sublime.packages_path(),
            "User",
            "RemoteEdit",
            "Cats",
            "%s.cat" % self.serverName
        )
        localFolder = self.get_local_tmp_path()
        try:
            os.makedirs(localFolder)
        except FileExistsError:
            # Directory already exists
            pass
        except Exception as e:
            print("EXCEP WHEN MAKING LOCAL FOLDER: %s" % e)
            return False
        wCmd = self.get_command("plink.exe")
        plink = self.get_process(wCmd)
        self.await_response(plink)
        if plink["process"].poll() is not None:
            print("Error connecting to server %s" % self.serverName)
            return False

        # TODO, DONT BLINDLY CONNECT HERE EVEN THOUGH WE SEND 1
        if "host key is not cached in the registry" in self.lastErr:
            send = "n\n"
            plink["process"].stdin.write(bytes(send, "utf-8"))
            self.await_response(plink)

        # We should be at a prompt
        send = "cd %s && ls %s -R > /tmp/%sSub.cat 2>/dev/null; cd /tmp && rm %sSub.tar.gz; tar cfz %sSub.tar.gz %sSub.cat && rm %sSub.cat && echo $((666 + 445));\n" % (
            self.get_server_setting("cat_path"),
            self.lsParams,
            self.serverName,
            self.serverName,
            self.serverName,
            self.serverName,
            self.serverName
        )
        plink["process"].stdin.write(bytes(send, "utf-8"))
        self.await_response(plink)
        if not "1111" in self.lastOut:
            # Try 1 more time as it pauses when running the command
            # so we stop capturing
            for i in range(10):
                time.sleep(.3)
                # TODO: Do we need this for loop anymore now that ls is silent?????
                self.await_response(plink)
                if "1111" in self.lastOut:
                    break
                else:
                    print("Not found!")
        try:
            plink["process"].terminate()
        except:
            pass

        # Now grab the file
        wCmd = self.get_command("psftp.exe")
        psftp = self.get_process(wCmd)
        self.await_response(psftp)
        if "psftp>" not in self.lastOut:
            return False
        fileName = "%sSub.tar.gz" % self.serverName
        filePath = "/tmp/%s" % fileName
        localFile = os.path.join(
            localFolder,
            fileName
        )
        cmd = "get %s %s\n" % (filePath, localFile)
        psftp = self.run_sftp_command(psftp, cmd)
        if not self.sftpResult:
            return False
        # delete tmp file from server
        cmd = "del %s\n" % (fileName)
        psftp = self.run_sftp_command(psftp, cmd)
        if not self.sftpResult:
            return False
        try:
            psftp["process"].terminate()
        except:
            pass
        # check local file exists
        try:
            f = tarfile.open(localFile, "r:gz")
            f.extractall(localFolder)
            f.close()
        except Exception as e:
            print("GZIP EXC %s" % e)
            return False
        catDataFile = os.path.join(
            localFolder,
            "%sSub.cat" % self.serverName
        )
        struc = self.create_cat(
            catDataFile,
            self.get_server_setting("cat_path")
        )
        # delete local files
        os.remove(localFile)
        os.remove(catDataFile)
        # Save the python dict
        f = open(self.catFile, "wb")
        pickle.dump(struc, f)
        f.close()
        print("CATALOGUE'D")

    def create_cat(self, fileName, startAt):
        # Build our catalogue dictionary from one big recursive ls of the root
        # folder. The structure will be something like:
        #
        # struc["/"]["server"] = server name
        # struc["/"]["created"] = unixtime created
        # struc["/"]["updated"] = unixtime updated
        # struc["/"]["users"] = users dict int -> user name
        # struc["/"]["groups"] = groups dict int -> group name
        # struc["folder1"]["/"] = [list of stat info on folder 1]
        # struc["folder1"]["folder2"]["/"] = [list of stat info on folder 2]
        #
        # Stat info is a list of data:
        # [0] - 0 = file, 1 = folder, 2 = symlink to file, 3 = symlink to folder
        # [1] - convert to octal for file perms
        # [2] - key of user dict to convert this id to a string user name
        # [3] - key of group dict to convert this id to a string group name
        # [4] - filesize in bytes
        # [5] - date as unixtime
        # [6] - if symlink then note where it links to
        struc = {}
        catFile = open(fileName, "r", encoding="utf-8", errors="ignore")
        struc = self.parse_ls(struc, catFile.read(), startAt)
        catFile.close()
        return struc

    def parse_ls(self, struc, catFile, startAt, userDict=[], groupDict=[]):
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
                        self.permsLookup["%s%s%s" % (x, y, z)] = int("%s%s%s" % (
                            tmp[x],
                            tmp[y],
                            tmp[z]
                        ), 8)
        if "/" in struc and "users" in struc["/"]:
            userDict = struc["/"]["users"]
            groupDict = struc["/"]["groups"]
        tmpStruc = struc
        tmpStartStruc = struc
        for f in filter(bool, startAt.split('/')):
            if f not in tmpStartStruc:
                tmpStartStruc[f] = {}
            tmpStartStruc = tmpStartStruc[f]
        userDict = []
        groupDict = []
        f_f_fresh = False
        for line in catFile.split("\n"):
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
                tmpStruc = tmpStartStruc
                for f in filter(bool, key.split('/')):
                    if f not in tmpStruc:
                        tmpStruc[f] = {}
                    tmpStruc = tmpStruc[f]
                for o in options:
                    tmpStruc[o] = options[o]
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
                # As it may contain spaces we cheat to get the file name once we
                # have hit our "." current directory. Try to make this fairly
                # robust
                if name == "." and len(options) is 0:
                    charsIn1 = line.find("./")
                # Verify that with the ".." up a dir
                elif name == ".." and len(options) is 0:
                    charsIn2 = line.find("../")
                elif len(sl) < 5:
                    # Skip the "Total BYTES" message
                    pass
                elif not charsIn1 or not charsIn2 or charsIn1 != charsIn2:
                    print("ERROR PARSING LS OUTPUT on line: %s" % line)
                else:
                    cName = line[charsIn1:].strip()
                    if sl[0][0] == "l" and "->" in cName:
                        (cName, symlinkDest) = cName.split(" -> ")
                        if symlinkDest[0] != "/":
                            symlinkDest = self.join_path(self.join_path(
                                startAt,
                                key),
                                symlinkDest
                            )
                if len(sl) >= 7 and cName:
                    cName = cName.rstrip("/")
                    # If we have a full row of info and we're not a folder up (..)
                    # or current folder reference then add to our dict
                    tmpT = sl[0][0]
                    if tmpT == "-":
                        t = 0
                    elif tmpT == "d":
                        t = 1
                    elif tmpT == "l":
                        t = 2
                    elif tmpT in ["c", "b"]:
                        continue
                    else:
                        print("UNKNOWN FILE TYPE: %s" % tmpT)
                    try:
                        peaky = sl[0][1:10]
                        p = self.permsLookup[peaky]
                    except:
                        try:
                            peaky = sl[0][1:10].replace("s", "x").replace("t", "x")
                            p = self.permsLookup[peaky]
                        except:
                            # TODO: Not sure what to do with this, this will
                            # do for now
                            print(
                                "Couldn't parse perms string: %s. Skipping."
                                % sl[0][1:10]
                            )
                            continue
                    if sl[2] not in userDict:
                        userDict.append(sl[2])
                    u = userDict.index(sl[2])
                    if sl[3] not in groupDict:
                        groupDict.append(sl[3])
                    g = groupDict.index(sl[3])
                    s = int(sl[4])
                    try:
                        d = int(time.mktime(time.strptime(
                            "%s %s" % (sl[5], sl[6]),
                            "%Y-%m-%d %H:%M"
                        )))
                    except:
                        print("Error parseing date / time for line: %s" % line)
                        continue
                    stats = [t, p, u, g, s, d]
                    # If we have a symlink
                    if t is 2:
                        stats.append(symlinkDest)
                    # print(cName, stats)
                    options[cName] = {}
                    options[cName]["/"] = stats
        # Put our final dict of folder contents onto the main dict
        tmpStruc = tmpStartStruc
        for f in filter(bool, key.split('/')):
            if f not in tmpStruc:
                tmpStruc[f] = {}
            tmpStruc = tmpStruc[f]
        for o in options:
            if o in tmpStruc:
                tmpStruc[o]["/"] = options[o]["/"]
            else:
                tmpStruc[o] = options[o]
        # add user and group shizzle
        if "/" not in struc:
            struc["/"] = {}
        struc["/"]["server"] = self.serverName
        if "created" not in struc["/"]:
            struc["/"]["created"] = int(time.time())
        struc["/"]["updated"] = int(time.time())
        struc["/"]["users"] = userDict
        struc["/"]["groups"] = groupDict
        return struc

    def get_command(self, app):
        cmd = [
            os.path.join(self.get_bin_path(), app),
            "-agent",
            self.get_server_setting("host"),
            "-l",
            self.get_server_setting("user")
        ]
        if "psftp" not in app:
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

    def get_process(self, cmd):
        kwargs = {}
        if subprocess.mswindows:
            su = subprocess.STARTUPINFO()
            su.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            su.wShowWindow = subprocess.SW_HIDE
            kwargs['startupinfo'] = su
        # print(cmd)
        pq = {}
        pq["process"] = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            close_fds=False,
            **kwargs
        )
        pq["queue_out"] = queue.Queue()
        pq["queue_err"] = queue.Queue()
        pq["pwd"] = None
        to = threading.Thread(target=enqueue_output, args=(pq["process"].stdout, pq["queue_out"]))
        te = threading.Thread(target=enqueue_output, args=(pq["process"].stderr, pq["queue_err"]))
        to.daemon = True
        te.daemon = True
        to.start()
        te.start()
        return pq

    def get_bin_path(self):
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

    def get_server_setting(self, key, default=None):
        try:
            val = self.server["settings"][key]
        except:
            val = default
        return val

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
            print(self.lastJsonifyError)
            return False

    def load_server_list(self):
        # Load all files in User/RemoteEdit/Servers folder
        serverList = []
        serverConfigPath = os.path.join(
            sublime.packages_path(),
            "User",
            "RemoteEdit",
            "Servers"
        )
        if not os.path.exists(serverConfigPath):
            try:
                os.makedirs(serverConfigPath)
            except:
                pass
        for root, dirs, files in os.walk(serverConfigPath):
            for filename in fnmatch.filter(files, "*"):
                serverName = filename[0:filename.rfind(".")]
                serverList.append(serverName)
                self.servers[serverName] = {}
                self.servers[serverName]["path"] = os.path.join(root, filename)
                self.servers[serverName]["settings"] = self.jsonify(
                    open(self.servers[serverName]["path"]).read()
                )
        return serverList

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
        if path[-1] is "/":
            path = path[0:-1]
        return os.path.split(path)

    def join_path(self, path, folder):
        if path[-1] is not "/":
            path = path + "/"
        if not folder:
            print(path, folder)
        elif folder[-1] is not "/":
            folder = folder + "/"
        return "%s%s" % (path, folder)

    def run_ssh_command(self, ssh, cmd=None, checkReturn="$", awaitResponse=1):
        ssh = self.connection(ssh, "plink.exe", checkReturn)
        if not ssh:
            return False
        # If Run the actual command
        if cmd and not self.send_command(ssh, cmd):
            return False
        buf = ""
        # If not found then try again. TODO: NEED TIMEOUT ON THIS + COO IT
        while awaitResponse > 0:
            self.await_response(ssh)
            buf += self.lastOut
            if checkReturn in self.lastOut:
                break
            awaitResponse -= 1
        self.lastOut = buf
        if checkReturn not in self.lastOut:
            return False
        return ssh

    def run_sftp_command(self, psftp, cmd=None, checkReturn="psftp>"):
        psftp = self.connection(psftp, "psftp.exe", checkReturn)
        if not psftp:
            return False
        # If Run the actual command
        if cmd and not self.send_command(psftp, cmd):
            return False
        self.await_response(psftp)
        if checkReturn not in self.lastOut:
            self.sftpResult = False
        else:
            self.sftpResult = True
        return psftp

    def connection(self, p, app, checkReturn="psftp>"):
        try:
            if p["process"].poll() is None:
                print("POLLING OK")
                return p
            else:
                print("POLLING FAIL BUT PROCESS PRESENT")
        except:
            print("POLLING EXCEPTION, PROCESS DEAD")
        # need to reconnect
        wCmd = self.get_command(app)
        p = self.get_process(wCmd)
        # print("OPENING: %s" % str(p))
        self.await_response(p)
        if checkReturn not in self.lastOut:
            print("CONNECT FAILED: %s" % self.lastOut)
            return False
        return p

    def send_command(self, p, cmd):
        try:
            print("SENDING CMD: %s" % cmd)
            p["process"].stdin.write(bytes("%s\n" % cmd, "utf-8"))
            return True
        except Exception as e:
            print("COMMAND FAILED: %s" % e)
            return False

    def await_response(self, pq):
        print("Waiting for output...")
        self.lastOut = self.lastErr = ""
        i = 0
        while True:
            (outB, errB) = self.read_pipes(pq)
            self.lastOut += str(outB)
            self.lastErr += str(errB)
            if pq["process"].poll() is not None:
                break
            elif (len(self.lastOut) or len(self.lastErr)) and not outB and not errB:
                i += 1
                if i > 10:
                    break
            time.sleep(0.01)
        if self.lastOut:
            print("---------- OUT ----------\n%s\n" % self.lastOut)
        if self.lastErr:
            print("--------- ERROR ---------\n%s\n" % self.lastErr)

    def read_pipes(self, pq):
        out = err = ""
        # read line without blocking
        try:
            err = pq["queue_err"].get_nowait()
        except queue.Empty:
            pass
        # read line without blocking
        try:
            out = pq["queue_out"].get_nowait()
        except queue.Empty:
            pass
        return (out, err)

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


def enqueue_output(out, queue):
    while True:
        line = out.read(1000)
        queue.put(str(line, "utf-8", errors="ignore"))
        if not len(line):
            break
