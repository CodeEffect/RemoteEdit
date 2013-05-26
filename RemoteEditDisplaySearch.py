# coding=utf-8
import sublime
import sublime_plugin


class RemoteEditDisplaySearchCommand(sublime_plugin.TextCommand):
    def run(self, edit, serverName="", findResults=""):
        results = self.view.window().new_file()
        results.set_scratch(True)
        results.set_name("Find Results on %s" % serverName)
        newRegion = sublime.Region(1, 0)
        results.set_syntax_file("Packages/Default/Find Results.hidden-tmLanguage")
        results.replace(edit, newRegion, findResults)
