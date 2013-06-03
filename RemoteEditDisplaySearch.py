# coding=utf-8
import sublime
import sublime_plugin

import re
import os


class RemoteEditDisplaySearchCommand(sublime_plugin.TextCommand):
    def run(self, edit, search="", serverName="", filePath="", baseDir=""):
        print("Displaying search")
        try:
            lf = open(filePath, "r", encoding="utf-8", errors="ignore")
            results = lf.read()
            lf.close()
        except:
            sublime.error_message("Error searching remote server")
        # Parse the results
        i = 0
        matches = 0
        files = {}
        inResult = False
        resultsText = []
        resultsText.append("Searching for \"%s\" on %s\n" % (
            search,
            serverName
        ))
        aroundLine = re.compile("\.\/(.+)-([0-9]+)-(.*)")
        resultLine = re.compile("\.\/(.+):([0-9]+):(.*)")
        for line in results.split("\n"):
            i += 1
            if i is 1:
                # First line is our search command
                continue
            if "111111999999" in line:
                # We're done
                break
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
                    resultsText.append("\n%s/%s:\n" % (
                        baseDir.rstrip("/"),
                        fileName
                    ))
                files[aroundMatch.group(1)] = True
            if aroundMatch:
                resultsText.append("  %s%s\n" % (
                    aroundMatch.group(2).ljust(5),
                    aroundMatch.group(3).rstrip()
                ))
            if resultMatch:
                matches += 1
                tmp = resultMatch.group(3).rstrip()
                rLine = ""
                if len(tmp) > 200:
                    startAt = 0
                    while tmp.find(search, startAt) != -1:
                        key = tmp.find(search, startAt)
                        rLine += "………%s………  " % tmp[max(key - 15, 0):max(key + len(search) + 15, 0)]
                        startAt = key + len(search)
                else:
                    rLine = tmp
                ln = "%s:" % resultMatch.group(2)
                resultsText.append("  %s%s\n" % (
                    ln.ljust(5),
                    rLine
                ))
        resultsText.append("\n%s matche%s across %s file%s\n\n" % (
            matches,
            "" if matches is 1 else "s",
            len(files),
            "" if len(files) is 1 else "s"
        ))
        try:
            os.remove(filePath)
        except:
            pass
        # Open a new tab
        results = self.view.window().new_file()
        results.settings().set("reResults", "SET")
        results.settings().set("serverName", serverName)
        results.set_scratch(True)
        results.set_name("Find Results on %s. CTRL + double click to open the result." % serverName)
        newRegion = sublime.Region(1, 0)
        results.set_syntax_file("Packages/Default/Find Results.hidden-tmLanguage")
        results.replace(edit, newRegion, "".join(resultsText))
        print(results.settings().get("reResults"), self.view.settings().get("reResults"))
