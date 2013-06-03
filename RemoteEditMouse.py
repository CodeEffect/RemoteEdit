import sublime
import sublime_plugin

import re


class RemoteEditMouseCommand(sublime_plugin.TextCommand):
    def get_result_region(self, pos):
        line = self.view.line(pos)
        return line

    def run(self, edit):
        if not self.view.settings().get("reResults"):
            return
        serverName = self.view.settings().get("serverName")
        ## get selected line
        pos = self.view.sel()[0].end()
        result = self.get_result_region(pos)
        line = self.view.substr(sublime.Region(result.a, result.b))
        # Look for a line number
        lineRe = re.compile("\s([0-9]+):{0,1}\s*.*")
        lineNumberMatch = re.search(lineRe, line)
        if not lineNumberMatch:
            return
        lineNumber = lineNumberMatch.group(1)
        # Now work back until we hit a file name
        fileName = ""
        searchPos = result.a - 1
        fileRe = re.compile("^(/.*):$")
        while not fileName:
            result = self.get_result_region(searchPos)
            line = self.view.substr(sublime.Region(result.a, result.b))
            fileMatch = re.search(fileRe, line)
            if fileMatch:
                fileName = fileMatch.group(1)
            searchPos = max(result.a - 1, 0)
            if not searchPos:
                break
        if not fileName:
            return
        print(serverName, fileName, lineNumber)
        # Now just to open fileName at lineNumber
        self.view.window().run_command("remote_edit", {
            "serverName": serverName,
            "fileName": fileName,
            "lineNumber": lineNumber
        })
