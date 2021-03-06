# Remote Edit #

A plugin for Sublime Text 3 for editing files over SFTP. Works from Windows, Mac and Linux.

## Details ##

This plugin allows you to configure a list of *nix servers and connect to them over SSH / SFTP to edit and manage files. To make browsing and searching as fast as possible the plugin creates a local cache of the files, allowing fuzzy file name searching of your server in the manner of CTRL+P against local files. The plugin also allows you to search for text within remote files, presenting the results in the same way that CTRL+SHIFT+F does.

## IMPORTANT - Please read ##
Due to the relative infancy of the plugin the code is still under active development.

Because of this (and because you should anyway!) please ensure that you have good backups of any files that you will be working with. **Do not** use this plugin against production environments or anywhere where your uptime matters.

## Manual installation ##

At present the plugin is not in package control so you will need to install manually.

### Using GIT (recommended): ###
Go to the Packages directory (`Preferences` / `Browse Packages…`). Then clone this
repository:

    git clone git://github.com/CodeEffect/RemoteEdit

### Manually: ###
Download a zip of the project (click on the zip icon further up the github page) and extract
it into your packages directory (`Preferences` / `Browse Packages…`).

### Setting up your first server ###
Once the plugin is installed just tap `F5` and select the first option `Add a new server`. A new tab will open in sublime that allows you to tab between the various settings. When complete select save and you will be prompted to save the file into the correct directory.

Once saved, another tap of `F5` should allow you to select your server then browse it.

## Features ##
 - Connects to Linux, BSD and OSX remote hosts.
 - Assesses server OS and app versions on first connect allowing it to get the most out of each remote server.
 - List and browse remote files, optionally displaying extended file information such as permissions, owner, size and modified date.
 - Bookmark frequently used files and folders on a per-server basis.
 - Open, edit and save the same files seamlessly from within Sublime Text.
 - Sort file listing by file name, file extensions, last modified and file size.
 - Filter hidden files including VCS metadata.
 - Fast fuzzy file name search that replicates CTRL+P against remote servers.
 - Search inside files by running a search on the remote server. Results are presented as current CTRL+SHIFT+F results are. CTRL + double click will open the file and take you to the appropriate line.
 - Create new files and folders. Chmod, chown, rename, delete, move and copy existing ones.
 - Compress individual files or recursively against whole directories. Zip, bzip, gzip or lzma should all be available if your platform supports them. The compressed file can optionally be scheduled to download after creation.
 - SFTP only mode with reduced functionality to ensure that you will always be able to connect and edit.
 - Offers tail functionality allowing you to have your apache log file open in its own tab, live updating while you work.
 - Can present server uptime and load information in the Sublime status bar.


## Known Issues ##

 - It totally spanks your .bash_history with ls's
 - As SSH is used for some functionality and app versions / command line switches differ there are some issues with certain switches not being supported. Whilst this is becominging much less frequent, if it does happen to you, please take the time to report any issues that you do have. The more you are able to help out, the better the plugin will become.

## Default key bindings ##

`f5` - Show the main menu
`ctrl+shift+f5` - Open the fuzzy file name browser

## License ##

Remote Edit is licensed under the MIT license.

  Copyright (c) 2013 Steven Perfect <steve@codeeffect.co.uk>

  Permission is hereby granted, free of charge, to any person obtaining a copy
  of this software and associated documentation files (the "Software"), to deal
  in the Software without restriction, including without limitation the rights
  to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
  copies of the Software, and to permit persons to whom the Software is
  furnished to do so, subject to the following conditions:

  The above copyright notice and this permission notice shall be included in
  all copies or substantial portions of the Software.

  THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
  IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
  FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
  AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
  LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
  OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
  THE SOFTWARE.
