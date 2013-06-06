# coding=utf-8
import sublime_plugin
import sublime

import os
import time


class RemoteEditEvents(sublime_plugin.EventListener):
    def on_pre_save_async(self, view):
        reData = view.settings().get("reData", None)
        if reData:
            reData["local_save"] = time.time()
            view.settings().set("reData", reData)
            view.window().run_command("remote_edit", {"save": view.id()})

    def on_pre_close(self, view):
        reData = view.settings().get("reData", None)
        if reData:
            filePath = view.file_name()
            tmp = os.path.expandvars("%temp%")
            # Check it has been remotely saved, ok/cancel if not
            if filePath and tmp in filePath and os.path.exists(filePath):
                deleteMe = True
                localSave = 0
                remoteSave = 0
                if "local_save" in reData:
                    localSave = reData["local_save"]
                if "remote_save" in reData:
                    remoteSave = reData["remote_save"]
                if view.is_dirty():
                    # User will be prompted to save it anyway
                    # Loop until local_save is set
                    # then give it 30 seconds for remote save to be set
                    # if it doesn't happen then show an error message
                    # if it does then delete the file
                    #
                    # TODO: IS THIS REQUIRED???
                    pass
                elif remoteSave < localSave:
                    # A save has failed at some point but the view is not
                    # currently dirty.
                    view.window().run_command(
                        "remote_edit",
                        {"action": "save", "save": view.id()}
                    )
                    # Kick off the save and wait. If not successful display an
                    # error message. Otherwise delete
                    wait = 30
                    deleteMe = False
                    while wait > 0:
                        reData = view.settings().get("reData", None)
                        if "local_save" in reData:
                            localSave = reData["local_save"]
                        if "remote_save" in reData:
                            remoteSave = reData["remote_save"]
                        if remoteSave >= localSave:
                            deleteMe = True
                            break
                        time.sleep(1)
                        wait -= 1
                if deleteMe:
                    sublime.set_timeout(
                        lambda: os.remove(filePath),
                        1000
                    )
                else:
                    fileName = os.path.split(filePath)[1]
                    sublime.error_message(
                        "File \"%s\" has unsaved remote modifications. You may find a local copy at \"%s\"" % (
                            fileName,
                            filePath
                        )
                    )
                    print("ERROR, REMOTE FILE %s UNSAVED" % fileName)
