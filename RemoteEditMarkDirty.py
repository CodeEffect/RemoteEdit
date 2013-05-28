# coding=utf-8
import sublime
import sublime_plugin


class RemoteEditMarkDirtyCommand(sublime_plugin.TextCommand):
    def run(self, edit, id=None):
        if id != self.view.id():
            return sublime.error_message("Error marking file as dirty")
        regionPart = sublime.Region(0, 1)
        regionText = self.view.substr(regionPart)
        self.view.replace(edit, regionPart, regionText)
