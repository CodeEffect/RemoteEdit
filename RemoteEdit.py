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
    catalogFile = False
    catalog = None
    forceReloadCatalog = True
    lastDir = None
    mode = "edit"
    info = False
    showHidden = False
    dontEditExt = []
    dontCatalogFolders = []
    psftp = False

    def run(self, action=None):
        print(self.serverName)
        if self.serverName:
            self.startServer(self.serverName)
        else:
            # List servers
            self.items = self.loadServerList()
            items = []
            for name in self.servers:
                items.append([
                    "%s (%s)" % (name, self.servers[name]["settings"]["host"]),
                    "User: %s, Path: %s" % (
                        self.servers[name]["settings"]["user"],
                        self.servers[name]["settings"]["remote_path"]
                    )
                ])
            items.insert(0, [
                " • Quick connect",
                "Just enter a host and a username / password"
            ])
            items.insert(0, [
                " • Add a new server",
                "Complete new server details to quickly connect in future"
            ])
            self.show_quick_panel(items, self.handleServerSelect)

        # TODO: MORE FOR 'RON
        #
        # Search in files GREP IT!
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
        # sort out psftp and plink var names
        #
        # TODO: Fix remote_path = "/" for "/" and "/var/"

    def handleServerSelect(self, selection):
        if selection is -1:
            return
        if selection is 0:
            # User has requested to add a new server
            # TODO ADD NEW SERVER
            sublime.error_message("TODO")
        if selection is 1:
            # User has requested to quick connect
            self.show_input_panel(
                "Enter connection string (user@hostname:port/remote/path): ",
                "",
                self.handleQuickHost,
                self.handleChange,
                self.handleCancel
            )
        else:
            self.startServer(self.items[selection - 2])

    def startServer(self, serverName, quickConnect=False):
        try:
            self.forceReloadCatalog = bool(self.serverName != serverName)
            self.serverName = serverName
            self.dontEditExt = self.getSettings().get(
                "dont_edit_ext",
                []
            )
            self.dontCatalogFolders = self.getSettings().get(
                "dont_catalog_folders",
                []
            )
            if not quickConnect:
                self.server = self.servers[self.serverName]
                self.dontEditExt = self.getServerSetting(
                    "dont_edit_ext",
                    self.dontEditExt
                )
                self.dontCatalogFolders = self.getServerSetting(
                    "dont_catalog_folders",
                    self.dontCatalogFolders
                )
        except:
            self.serverName = None
            self.run()
            return
            # self.errorMessage("ERROR! Server \"%s\" not found." % serverName)

        # K, fire up a thread to pull down an ls and process it
        # meanwhile open a connection to the server and present the user with a
        # filelist etc
        self.catalogServer()

        # list files
        self.openServer()

    def openServer(self):
        reData = self.window.active_view().settings().get("reData", None)
        if reData and self.serverName == reData["serverName"]:
            if "browse_path" in reData:
                self.lastDir = reData["browse_path"]
            else:
                self.lastDir = reData["path"]
        elif not self.lastDir:
            self.lastDir = self.getServerSetting(
                "remote_path",
                "/home/%s" % self.getServerSetting("user")
            )
        s = self.listDirectory(self.lastDir)
        if not s:
            # error message
            return self.errorMessage(
                "Error connecting to %s" % self.serverName,
                True
            )
        # Show the options
        self.show_quick_panel(self.items, self.handleList)

    def handleQuickHost(self, connectionString):
        self.server = {}
        self.server["settings"] = {}
        if "/" in connectionString:
            (connectionString, self.server["settings"]["remote_path"]) = connectionString.split("/", 1)
            self.server["settings"]["remote_path"] = "/" + self.server["settings"]["remote_path"]
        else:
            self.server["settings"]["remote_path"] = "/"
        if ":" in connectionString:
            (connectionString, self.server["settings"]["port"]) = connectionString.split(":")
        else:
            self.server["settings"]["port"] = "22"
        if "@" in connectionString:
            (self.server["settings"]["user"], self.server["settings"]["host"]) = connectionString.split("@")
        else:
            self.server["settings"]["user"] = "root"
            self.server["settings"]["host"] = connectionString

        self.serverName = self.server["settings"]["host"]
        self.show_input_panel(
            "Enter password (blank to attempt pageant auth: ",
            "",
            self.handleQuickPassword,
            self.handleChange,
            self.handleCancel
        )

    def handleQuickPassword(self, password):
        self.server["settings"]["password"] = password
        self.startServer(self.serverName, True)

    def closeApps(self):
        try:
            self.psftp["process"].terminate()
        except:
            pass
        try:
            self.plink["process"].terminate()
        except:
            pass

    def handleFuzzy(self, selection):
        if selection == -1:
            self.closeApps()
            return
        (self.lastDir, selected) = self.splitPath(self.items[selection][1])
        self.maintainOrDownload(selected)

    def handleGrep(self, search):
        print(search)
        if not search:
            return self.show_quick_panel(self.items, self.handleList)
        wCmd = self.getCommand("plink.exe")
        self.plink = self.getProcess(wCmd)
        self.awaitResponse(self.plink)
        if self.plink["process"].poll() is not None:
            print("Error connecting to server %s" % self.serverName)
            return False
        # We should be at a prompt
        cmd = "cd %s && grep -nR -A2 -B2 %s .; echo %s;" % (
            self.lastDir,
            search,
            "\"GREPPING\" $((66666 + 44445)) \"GREPGREPGREPGREPGREPALOT\""
        )
        checkReturn = "111111"
        if not self.runSshCommand(self.plink, cmd, checkReturn):
            return self.commandError(cmd)
        # here we parse the results
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
        for line in self.lastOut.split("\n"):
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
        self.window.run_command(
            "remote_edit_display_search",
            {
                "findResults": "".join(resultsText),
                "serverName": self.serverName
            }
        )

    def handleList(self, selection):
        if selection == -1:
            self.closeApps()
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
                self.handleNavigate,
                self.handleChange,
                self.handleCancel
            )
        elif selection == 1:
            # Folder options
            (head, tail) = self.splitPath(self.lastDir)
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
            self.show_quick_panel(self.folderOptions, self.handleFolderOptions)
        elif selection == 2 or selected[-1] == "/":
            # Up a folder
            if selection == 2:
                if len(self.lastDir) <= 1:
                    self.lastDir = "/"
                else:
                    (head, tail) = self.splitPath(self.lastDir)
                    if len(head) is 1:
                        self.lastDir = "/"
                    else:
                        self.lastDir = "%s/" % head
            else:
                self.lastDir = self.joinPath(
                    self.lastDir,
                    selected
                )
            s = self.listDirectory(self.lastDir)
            if not s:
                # error message
                return self.errorMessage(
                    "Error changing folder to %s" % self.lastDir
                )
            else:
                # Show the options
                self.show_quick_panel(self.items, self.handleList)
        else:
            self.maintainOrDownload(selected)

    def maintainOrDownload(self, selected):
        ext = selected.split(".")[-1]
        if self.mode == "edit" and ext not in self.dontEditExt:
            if not self.downloadAndOpen(selected):
                return self.errorMessage("Error downloading %s" % selected)
        else:
            # give options
            # rename, chmod, chown, delete
            downloadFolder = os.path.expandvars(
                self.getSettings().get(
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
            self.show_quick_panel(items, self.handleMaintenance)

    def handleMaintenance(self, selection):
        if selection == 0:
            if not self.downloadAndOpen(self.selected):
                return sublime.error_message(
                    "Error connecting to %s" % self.serverName
                )
        elif selection == 1:
            caption = "Rename to: "
            self.show_input_panel(
                caption,
                self.selected,
                self.handleRename,
                self.handleChange,
                self.showList
            )
        elif selection == 2:
            #TODO
            caption = "Move to: "
            self.show_input_panel(
                caption,
                self.selected,
                self.handleRename,
                self.handleChange,
                self.showList
            )
        elif selection == 3:
            #TODO
            caption = "Copy to: "
            self.show_input_panel(
                caption,
                self.selected,
                self.handleRename,
                self.handleChange,
                self.showList
            )
        elif selection == 4 or selection == 5:
            # Save file to download folder
            downloadFolder = os.path.expandvars(
                self.getSettings().get(
                    "download_folder",
                    "%UserProfile%\\Downloads"
                )
            )
            if not self.downloadFileTo(self.selected, downloadFolder):
                return self.errorMessage("Error downloading %s" % self.selected)
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
                self.handleRename,
                self.handleChange,
                self.showList
            )
        elif selection == 7:
            caption = "chmod to: "
            perms = self.getPerms(self.selected)
            self.show_input_panel(
                caption,
                perms,
                self.handleChmod,
                self.handleChange,
                self.showList
            )
        elif selection == 8:
            caption = "chown to: "
            (user, group) = self.getUserAndGroup(self.selected)
            self.show_input_panel(
                caption,
                "%s:%s" % (user, group),
                self.handleChown,
                self.handleChange,
                self.showList
            )
        elif selection == 9:
            if self.ok_cancel_dialog(
                "Are you sure you want to delete %s" % self.selected,
                "Delete"
            ):
                # TODO: DELETE FILE
                pass

    def handleRename(self, fileName):
        if self.selected is -1:
            (head, tail) = self.splitPath(self.lastDir)
            cmd = "cd %s" % head
            if not self.runSftpCommand(self.psftp, cmd):
                return self.commandError(cmd)
        else:
            head = self.lastDir
            tail = self.selected
        if tail != fileName:
            cmd = "mv %s %s" % (tail, fileName)
            if not self.runSftpCommand(self.psftp, cmd):
                return self.commandError(cmd)
            else:
                # TODO: UPDATE LOCAL!!!!!!!!!!!!!!!!!
                # PATHS WILL NOT BE CORRECT
                if self.selected is -1:
                    self.lastDir = self.joinPath(head, fileName)
        self.show_quick_panel(self.items, self.handleList)

    def handleChmod(self, chmod):
        # TODO: VALIDATE CHMOD
        if self.selected is -1:
            fileName = self.lastDir
        else:
            fileName = self.selected
        cmd = "chmod %s %s" % (chmod, fileName)
        if not self.runSftpCommand(self.psftp, cmd):
            return self.commandError(cmd)
        else:
            self.show_quick_panel(self.items, self.handleList)

    def handleChown(self, chown):
        # TODO: VALIDATE CHOWN
        if self.selected is -1:
            fileName = self.lastDir
        else:
            fileName = self.selected
        # TODO: CHOWN DOESN'T RUN FROM SFTP!!!!
        # UPDATE LOCAL!!!!!!!!!!!!!
        cmd = "chown %s %s" % (chown, fileName)
        if not self.runSftpCommand(self.psftp, cmd):
            return self.commandError(cmd)
        else:
            self.show_quick_panel(self.items, self.handleList)

    def showList(self):
        self.show_quick_panel(self.items, self.handleList)

    def getUserAndGroup(self, fileName):
        user = None
        group = None
        stats = self.getFileStats(self.joinPath(self.lastDir, fileName))
        try:
            user = self.catalog["/"]["users"][stats[2]]
            group = self.catalog["/"]["group"][stats[3]]
        except:
            #TODO connect in to the server and get them
            pass
        return (user, group)

    def getPerms(self, fileName):
        stats = self.getFileStats(self.joinPath(self.lastDir, fileName))
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

    def getFileStats(self, filePath):
        f = self.getFileFromCatalog(filePath)
        if not f:
            # Connect to server and get info
            pass
        return f["/"]

    def getFileFromCatalog(self, filePath):
        tmp = self.catalog
        try:
            for f in filter(bool, filePath.split("/")):
                tmp = tmp[f]
            return tmp
        except:
            return False

    def appendFilesFromPath(self, fileDict, filePath):
        for f in fileDict:
            if f != "/":
                if self.showHidden or (not self.showHidden and f[0] != "."):
                    if fileDict[f]["/"][0] == 0:
                        self.items.append([f, self.joinPath(filePath, f)])
                    else:
                        self.appendFilesFromPath(
                            fileDict[f],
                            self.joinPath(filePath, f)
                        )

    def handleNavigate(self, path):
        prevDir = self.lastDir
        self.lastDir = path
        s = self.listDirectory(path)
        if not s:
            self.lastDir = prevDir
            # error message
            sublime.error_message(
                "Path \"%s\" not found" % path
            )
        # Show the options
        self.show_quick_panel(self.items, self.handleList)

    def handleFolderOptions(self, selection):
        print(selection)
        if selection == -1:
            self.closeApps()
            return
        elif selection == 0:
            # Back to prev list
            self.show_quick_panel(self.items, self.handleList)
        elif selection == 1:
            # Fuzzy file name from here
            # TODO: ONLY SHOW THIS IF WE HAVE A CATALOGUE
            self.items = []
            self.appendFilesFromPath(
                self.getFileFromCatalog(self.lastDir),
                self.lastDir
            )
            self.show_quick_panel(self.items, self.handleFuzzy)
        elif selection == 2:
            # Search within files from here
            caption = "Enter search term"
            print("SHOW GREO")
            self.show_input_panel(
                caption,
                "",
                self.handleGrep,
                self.handleChange,
                self.showList
            )
        elif selection == 3:
            # new file
            caption = "Enter file name: "
            self.show_input_panel(
                caption,
                "",
                self.handleNewFile,
                self.handleChange,
                self.showList
            )
        elif selection == 4:
            # new folder
            caption = "Enter folder name: "
            self.show_input_panel(
                caption,
                "",
                self.handleNewFolder,
                self.handleChange,
                self.showList
            )
        elif selection == 5:
            # rename
            caption = "Enter new name: "
            self.selected = -1
            (head, tail) = self.splitPath(self.lastDir)
            self.show_input_panel(
                caption,
                tail,
                self.handleRename,
                self.handleChange,
                self.showList
            )
        elif selection == 6:
            # move
            # TODO: Select new path with quick panel
            caption = "Enter new name: "
            self.selected = -1
            (head, tail) = self.splitPath(self.lastDir)
            self.show_input_panel(
                caption,
                tail,
                self.handleRename,
                self.handleChange,
                self.showList
            )
        elif selection == 7:
            # copy
            # #TODO
            caption = "Enter new name: "
            self.selected = -1
            (head, tail) = self.splitPath(self.lastDir)
            self.show_input_panel(
                caption,
                tail,
                self.handleRename,
                self.handleChange,
                self.showList
            )
        elif selection == 8:
            # zip
            # TODO
            caption = "Enter new name: "
            self.selected = -1
            (head, tail) = self.splitPath(self.lastDir)
            self.show_input_panel(
                caption,
                tail,
                self.handleRename,
                self.handleChange,
                self.showList
            )
        elif selection == 9:
            # chmod
            self.selected = -1
            caption = "chmod to: "
            perms = self.getPerms(self.selected)
            self.show_input_panel(
                caption,
                perms,
                self.handleChmod,
                self.handleChange,
                self.showList
            )
        elif selection == 10:
            # chown
            self.selected = -1
            caption = "chown to: "
            (user, group) = self.getUserAndGroup(self.selected)
            self.show_input_panel(
                caption,
                "%s:%s" % (user, group),
                self.handleChown,
                self.handleChange,
                self.showList
            )
        elif selection == 11:
            # delete
            self.selected = -1
            (head, tail) = self.splitPath(self.lastDir)
            if self.ok_cancel_dialog(
                "Are you sure you want to delete %s" % tail,
                "Delete"
            ):
                # TODO: DELETE FILE
                pass
        elif selection == 12:
            # Show / hide hidden files
            self.showHidden = self.showHidden is False
            self.listDirectory(self.lastDir)
            self.show_quick_panel(self.items, self.handleList)
        elif selection == 13:
            # edit mode
            self.mode = "edit"
            self.listDirectory(self.lastDir)
            self.show_quick_panel(self.items, self.handleList)
        elif selection == 14:
            # maintenance mode
            self.mode = "maintenance"
            self.listDirectory(self.lastDir)
            self.show_quick_panel(self.items, self.handleList)
        elif selection == 15:
            # Turn on / off extended file / folder info
            self.info = self.info is False
            self.listDirectory(self.lastDir)
            self.show_quick_panel(self.items, self.handleList)
        elif selection == 16:
            # Disconnect from this server
            self.serverName = None
            self.closeApps()
            self.run()
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
            else:
                # TODO: MAKE LOCAL FILE, SET SFTP FLAGS
                # OPEN IN EDITOR
                pass

    def handleNewFolder(self, folderName):
        if not folderName:
            self.show_quick_panel(self.folderOptions, self.handleFolderOptions)
        else:
            cmd = "mkdir %s" % folderName
            if not self.runSftpCommand(self.psftp, cmd):
                return self.commandError(cmd)
            self.lastDir = self.joinPath(self.lastDir, folderName)
            # cmd = "cd %s" % self.lastDir
            # if not self.runSftpCommand(self.psftp, cmd):
            #     return self.commandError(cmd)
        self.items = []
        self.addOptionsToItems()
        self.show_quick_panel(self.items, self.handleList)

    def addOptionsToItems(self):
        if self.info:
            (head, tail) = self.splitPath(self.lastDir)
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
            ), self.getServerSetting("host")])
        else:
            self.items.insert(0, ".. Up a folder")
            self.items.insert(0, " • Folder Actions / Settings [%s mode]" % self.mode.capitalize())
            self.items.insert(0, "%s:%s" % (
                self.serverName,
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
        try:
            os.makedirs(localFolder)
        except FileExistsError:
            pass
        return localFolder

    def downloadAndOpen(self, f):
        localFolder = self.makeLocalFolder()
        if not localFolder:
            # error message
            self.lastErr = "Error creating local folder"
            return False
        # TODO ESCAPE FILENAMES!!!!!!!!!!!!!!
        remoteFile = self.joinPath(self.lastDir, f)
        localFile = os.path.join(localFolder, f)
        cmd = "get %s %s" % (remoteFile, localFile)
        if not self.runSftpCommand(self.psftp, cmd):
            return self.commandError(cmd)

        # TODO: SAVE A SETTING TO THE VIEW TO INDICATE THAT THE
        # FILE WAS OPENED WITH REMOTE EDIT + SERVER +  POSS OTHER
        # DETAILS??
        # THESE PERSIST BETWEEN APP RELOADS. W00T W00T
        reData = {
            "serverName": self.serverName,
            "fileName": f,
            "path": self.lastDir,
            "openedAt": time.time()
        }
        self.window.open_file("%s%s" % (localFolder, f))
        self.window.active_view().settings().set("reData", reData)
        return True

    def downloadFileTo(self, f, destination):
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
            if not self.runSftpCommand(self.psftp, cmd):
                return self.errorMessage("Error downloading %s" % f, True)
        cmd = "get %s %s" % (f, destFile)
        if not self.runSftpCommand(self.psftp, cmd):
            return self.errorMessage("Error downloading %s" % f, True)
        return True

    def listDirectory(self, d):
        self.items = []
        if self.catalog:
            # Display options based on the catalog and self.lastDir
            try:
                fldr = self.getFileFromCatalog(d)
                # print(fldr)
                if self.info:
                    for f in filter(bool, fldr):
                        # print(f)
                        if f != "/" and (self.showHidden or (not self.showHidden and f[0] != ".")):
                            self.items.append(
                                [
                                    "%s%s" % (f, "/" if fldr[f]["/"][0] == 1 else ""),
                                    "%s  %s %s %s %s" % (
                                        oct(fldr[f]["/"][1])[2:5],
                                        self.catalog["/"]["users"][fldr[f]["/"][2]],
                                        self.catalog["/"]["groups"][fldr[f]["/"][3]],
                                        "" if fldr[f]["/"][0] == 1 else " " + self.displaySize(fldr[f]["/"][4]) + " ",
                                        self.displayTime(fldr[f]["/"][5])
                                    )
                                ]
                            )
                else:
                    for f in filter(bool, fldr):
                        # print(f)
                        # if f == "lock":
                        #     print(fldr[f])
                        if f != "/" and (self.showHidden or (not self.showHidden and f[0] != ".")):
                            self.items.append(
                                "%s%s" % (
                                    f,
                                    "/" if fldr[f]["/"][0] == 1 else ""
                                )
                            )
            except Exception as e:
                print("%s NOT IN CATALOG: %s" % (d, e))
        if not self.items:
            cmd = "ls %s" % d
            if not self.runSftpCommand(self.psftp, cmd):
                return self.commandError(cmd)
            # parse out
            # TODO: ADD THIS TO CATALOG!!!!!!!!!!!!
            self.items = []
            for line in self.lastOut.split("\n"):
                la = line.split(" ")
                f = la[-1].strip()
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
        self.addOptionsToItems()
        return True

    def displayTime(self, uTime):
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(uTime))

    def displaySize(self, bytes):
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

    def catalogServer(self):
        if not self.getServerSetting("cache_file_structure"):
            return
        if not self.getServerSetting("remote_path"):
            return

        # TODO Coo this

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
        except:
            mTime = stale = 0
        if mTime + stale < time.time():
            # needs a refresh
            # todo, move this to a background thread
            # todo, set a flag for known hosts after first connect
            # if the flag is set we can punt the plink query straight into the
            # background thread without worrying about known hosts

            localFolder = os.path.join(
                os.path.expandvars("%temp%"),
                "RemoteEdit",
                self.serverName
            )
            try:
                os.makedirs(localFolder)
            except Exception as e:
                # TODO: If exception is not "folders present" then return false
                print("EXCEP WHEN MAKING LOCAL FOLDER: %s" % e)

            wCmd = self.getCommand("plink.exe")
            plink = self.getProcess(wCmd)
            self.awaitResponse(plink)
            if plink["process"].poll() is not None:
                print("Error connecting to server %s" % self.serverName)
                return False

            # TODO, DONT BLINDLY CONNECT HERE EVEN THOUGH WE SEND 1
            if "host key is not cached in the registry" in self.lastErr:
                send = "n\n"
                plink["process"].stdin.write(bytes(send, "utf-8"))
                self.awaitResponse(plink)

            # We should be at a prompt
                self.getServerSetting("remote_path"),
                self.serverName,
            send = "cd %s && ls -lapR --time-style=long-iso > /tmp/%sSub.cat 2>/dev/null; cd /tmp && rm %sSub.tar.gz; tar cfz %sSub.tar.gz %sSub.cat && rm %sSub.cat && echo $((666 + 445));\n" % (
                self.serverName,
                self.serverName,
                self.serverName,
                self.serverName
            )
            plink["process"].stdin.write(bytes(send, "utf-8"))
            self.awaitResponse(plink)
            if not "1111" in self.lastOut:
                # Try 1 more time as it pauses when running the command
                # so we stop capturing
                for i in range(10):
                    time.sleep(.3)
                    # TODO: Do we need this for loop anymore now that ls is silent?????
                    self.awaitResponse(plink)
                    if "1111" in self.lastOut:
                        break
                    else:
                        print("Not found!")
            try:
                plink["process"].terminate()
            except:
                pass

            # Now grab the file
            wCmd = self.getCommand("psftp.exe")
            psftp = self.getProcess(wCmd)
            self.awaitResponse(psftp)
            if "psftp>" not in self.lastOut:
                return False
            # cmd = "cd /tmp"
            # if not self.runSftpCommand(psftp, cmd):
            #     return False
            fileName = "%sSub.tar.gz" % self.serverName
            filePath = "/tmp/%s" % fileName
            localFile = os.path.join(
                localFolder,
                fileName
            )
            cmd = "get %s %s\n" % (filePath, localFile)
            self.psftp = self.runSftpCommand(self.psftp, cmd)
            if not self.psftp:
                print(self.psftp)
                return False
            # delete tmp file from server
            cmd = "del %s\n" % (fileName)
            self.psftp = self.runSftpCommand(self.psftp, cmd)
            if not self.psftp:
                print(self.psftp)
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
            struc = self.createCatalog(
                catDataFile,
                self.getServerSetting("remote_path")
            )
            # delete local files
            os.remove(localFile)
            os.remove(catDataFile)
            # Save the python dict
            f = open(self.catalogFile, "wb")
            pickle.dump(struc, f)
            f.close()
            print("CATALOG'D")
        if not self.catalog or self.forceReloadCatalog:
            print("RELOAD!")
            self.catalog = pickle.load(open(self.catalogFile, "rb"))

    def createCatalog(self, fileName, startAt):
        # Build our catalog dictionary from one big recursive ls of the root
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

        # Build a lookup dict for quickly converting rwxrwxrwx to an integer
        tmp = {}
        tmp["---"] = 0
        tmp["--x"] = 1
        tmp["-w-"] = 2
        tmp["-wx"] = 3
        tmp["r--"] = 4
        tmp["r-x"] = 5
        tmp["rw-"] = 6
        tmp["rwx"] = 7
        permsLookup = {}
        for x in tmp:
            for y in tmp:
                for z in tmp:
                    permsLookup["%s%s%s" % (x, y, z)] = int("%s%s%s" % (
                        tmp[x],
                        tmp[y],
                        tmp[z]
                    ), 8)
        struc = {}
        tmpStruc = struc
        tmpStartStruc = struc
        for f in filter(bool, startAt.split('/')):
            tmpStartStruc[f] = {}
            tmpStartStruc = tmpStartStruc[f]
        userDict = []
        groupDict = []
        f_f_fresh = False
        catFile = open(fileName, "r", encoding="utf-8", errors="ignore")
        for line in catFile:
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
                        if f in self.dontCatalogFolders:
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
                    cName = line[charsIn1:]
                    if sl[0][0] == "l" and "->" in cName:
                        (cName, symlinkDest) = cName.split(" -> ")
                        if symlinkDest[0] != "/":
                            symlinkDest = self.joinPath(self.joinPath(
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
                        p = permsLookup[peaky]
                    except:
                        try:
                            peaky = sl[0][1:10].replace("s", "x").replace("t", "x")
                            p = permsLookup[peaky]
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
                    options[cName] = {}
                    options[cName]["/"] = stats
        # Put our final dict of folder contents onto the main dict
        tmpStruc = tmpStartStruc
        for f in filter(bool, key.split('/')):
            if f not in tmpStruc:
                tmpStruc[f] = {}
            tmpStruc = tmpStruc[f]
        catFile.close()
        # add user and group shizzle
        struc["/"] = {}
        struc["/"]["server"] = self.serverName
        struc["/"]["created"] = int(time.time())
        struc["/"]["updated"] = int(time.time())
        struc["/"]["users"] = userDict
        struc["/"]["groups"] = groupDict
        return struc

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

    def handleChange(self, selection):
        return

    def handleCancel(self):
        return

    def splitPath(self, path):
        if path[-1] is "/":
            path = path[0:-1]
        return os.path.split(path)

    def joinPath(self, path, folder):
        if path[-1] is not "/":
            path = path + "/"
        if not folder:
            print(path, folder)
        elif folder[-1] is not "/":
            folder = folder + "/"
        return "%s%s" % (path, folder)

    def runSshCommand(self, ssh, cmd=None, checkReturn="$"):
        ssh = self.connection(ssh, "plink.exe", checkReturn)
        if not ssh:
            return False
        # If Run the actual command
        if cmd and not self.sendCommand(ssh, cmd):
            return False
        self.awaitResponse(ssh)
        if checkReturn not in self.lastOut:
            return False
        return ssh

    def runSftpCommand(self, psftp, cmd=None, checkReturn="psftp>"):
        psftp = self.connection(psftp, "psftp.exe", checkReturn)
        if not psftp:
            return False
        # If Run the actual command
        if cmd and not self.sendCommand(psftp, cmd):
            return False
        self.awaitResponse(psftp)
        if checkReturn not in self.lastOut:
            return False
        return psftp

    def connection(self, p, app, checkReturn="psftp>"):
        try:
            print("POLLING")
            if p["process"].poll() is None:
                print("POLLING OK")
                return p
            else:
                print("POLLING FAIL BUT p PRESENT")
        except:
            print("POLLING EXCEPTION, PROCESS DEAD")
        # need to reconnect
        wCmd = self.getCommand(app)
        p = self.getProcess(wCmd)
        # print("OPENING: %s" % str(p))
        self.awaitResponse(p)
        if "p>" not in self.lastOut:
            print("CONNECT FAILED: %s" % self.lastOut)
            return False
        return p

    def sendCommand(self, p, cmd):
        try:
            print("SENDING CMD: %s" % cmd)
            p["process"].stdin.write(bytes("%s\n" % cmd, "utf-8"))
            return True
        except Exception as e:
            print("COMMAND FAILED: %s" % e)
            return False

    def awaitResponse(self, pq):
        print("READ UNTIL READY CALLED")
        self.lastOut = self.lastErr = ""
        i = 0
        while True:
            (outB, errB) = self.readPipes(pq)
            self.lastOut += str(outB)
            self.lastErr += str(errB)
            if pq["process"].poll() is not None:
                break
            elif (len(self.lastOut) or len(self.lastErr)) and not outB and not errB:
                i += 1
                if i > 5:
                    break
            time.sleep(0.01)
        if self.lastOut:
            print("---------- OUT ----------\n%s\n" % self.lastOut)
        if self.lastErr:
            print("--------- ERROR ---------\n%s\n" % self.lastErr)

    def readPipes(self, pq):
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

    def commandError(self, cmd):
        sublime.error_message(
            "Command \"%s\" failed on %s" % (
                cmd,
                self.serverName
            )
        )
        return False

    def errorMessage(self, msg, useLastError=False):
        if useLastError and self.lastErr:
            return sublime.error_message(self.lastErr)
        sublime.error_message(msg)
        return False


def enqueue_output(out, queue):
    while True:
        line = out.read(1000)
        queue.put(str(line, "utf-8"))
        if not len(line):
            break


class RemoteEditDisplaySearchCommand(sublime_plugin.TextCommand):
    def run(self, edit, serverName="", findResults=""):
        results = self.view.window().new_file()
        results.set_scratch(True)
        results.set_name("Find Results on %s" % serverName)
        newRegion = sublime.Region(1, 0)
        results.set_syntax_file("Packages/Default/Find Results.hidden-tmLanguage")
        results.replace(edit, newRegion, findResults)
