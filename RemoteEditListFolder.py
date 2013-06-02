# coding=utf-8
import sublime
import sublime_plugin


class RemoteEditListFolderCommand(sublime_plugin.TextCommand):
    def run(self, edit, path="", contents=""):
        print("Listing folder %s" % path)
        results = self.view.window().new_file()
        results.set_name("Files and folders at %s" % path)
        newRegion = sublime.Region(1, 0)
        results.replace(edit, newRegion, contents)
