#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Name:
    gist

Usage:
    gist list
    gist edit <id>
    gist description <id> <desc>
    gist info <id>
    gist fork <id>
    gist files <id>
    gist delete <ids> ...
    gist archive <id>
    gist content <id> [<filename>] [--decrypt]
    gist create <desc> [--public] [--encrypt] [FILES ...]
    gist create <desc> [--public] [--encrypt] [--filename <filename>]
    gist clone <id> [<name>]
    gist version

Description:
    This program provides a command line interface for interacting with github
    gists.

Commands:
    create
        Create a new gist. A gist can be created in several ways. The content
        of the gist can be piped to the gist,

            $ echo "this is the content" | gist create "gist description"

        The gist can be created from an existing set of files,

            $ gist create "gist description" foo.txt bar.txt

        The gist can be created on the fly,

            $ gist create "gist description"

        which will open the users default editor.

        If you are creating a gist with a single file using either the pipe or
        'on the fly' method above, you can also supply an optional argument to
        name the file instead of using the default ('file1.txt'),

            $ gist create "gist description" --filename foo.md

        Note that the use of --filename is incompatible with passing in a list
        of existing files.

    edit
        You can edit your gists directly with the 'edit' command. This command
        will clone the gist to a temporary directory and open up the default
        editor (defined by the EDITOR environment variable) to edit the files
        in the gist. When the editor is exited the user is prompted to commit
        the changes, which are then pushed back to the remote.

    fork
        Creates a fork of the specified gist.

    description
        Updates the description of a gist.

    list
        Returns a list of your gists. The gists are returned as,

            2b1823252e8433ef8682 - mathematical divagations
            a485ee9ddf6828d697be - notes on defenestration
            589071c7a02b1823252e + abecedarian pericombobulations

        The first column is the gists unique identifier; The second column
        indicates whether the gist is public ('+') or private ('-'); The third
        column is the description in the gist, which may be empty.

    clone
        Clones a gist to the current directory. This command will clone any
        gist based on its unique identifier (i.e. not just the users) to the
        current directory.

    delete
        Deletes the specified gist.

    files
        Returns a list of the files in the specified gist.

    archive
        Downloads the specified gist to a temporary directory and adds it to a
        tarball, which is then moved to the current directory.

    content
        Writes the content of each file in the specified gist to the terminal,
        e.g.

            $ gist content c971fca7997aed65ddc9
            foo.txt:
            this is foo


            bar.txt:
            this is bar


        For each file in the gist the first line is the name of the file
        followed by a colon, and then the content of that file is written to
        the terminal.

        If a filename is given, only the content of the specified filename
        will be printed.

           $ gist content de42344a4ecb6250d6cea00d9da6d83a file1
           content of file 1


    info
        This command provides a complete dump of the information about the gist
        as a JSON object. It is mostly useful for debugging.

    version
        Returns the current version of gist.

"""

import codecs
import collections
import locale
import logging
import os
import platform
import struct
import sys
import tempfile

import docopt
import gnupg
import simplejson as json

from . import gist

if platform.system() != 'Windows':
    # those modules exist everywhere but on Windows
    import termios
    import fcntl

try:
    import configparser
except ImportError:
    import ConfigParser as configparser

# From version 3.2 readfp() has been deprecated and replaced by read_file().
# Here we monkeypatch earlier versions so that we have a consist interface.
if sys.version_info < (3, 2):
    configparser.ConfigParser.read_file = configparser.ConfigParser.readfp

logger = logging.getLogger('gist')

# We need to wrap stdout in order to properly handle piping uincode output
stream = sys.stdout.detach() if sys.version_info[0] > 2 else sys.stdout
encoding = locale.getpreferredencoding()
sys.stdout = codecs.getwriter(encoding)(stream)


class GistError(Exception):
    def __init__(self, msg):
        super(GistError, self).__init__(msg)
        self.msg = msg


class FileInfo(collections.namedtuple("FileInfo", "name content")):
    pass


def terminal_width():
    """Returns the terminal width

    Tries to determine the width of the terminal. If there is no terminal, then
    None is returned instead.

    """
    try:
        if platform.system() == "Windows":
            from ctypes import windll, create_string_buffer
            # Reference: https://docs.microsoft.com/en-us/windows/console/getstdhandle # noqa
            hStdErr = -12
            get_console_info_fmtstr = "hhhhHhhhhhh"
            herr = windll.kernel32.GetStdHandle(hStdErr)
            csbi = create_string_buffer(
                    struct.calcsize(get_console_info_fmtstr))
            if not windll.kernel32.GetConsoleScreenBufferInfo(herr, csbi):
                raise OSError("Failed to determine the terminal size")
            (_, _, _, _, _, left, top, right, bottom, _, _) = struct.unpack(
                    get_console_info_fmtstr, csbi.raw)
            tty_columns = right - left + 1
            return tty_columns
        else:
            exitcode = fcntl.ioctl(
                    0,
                    termios.TIOCGWINSZ,
                    struct.pack('HHHH', 0, 0, 0, 0))
            h, w, hp, wp = struct.unpack('HHHH', exitcode)
        return w
    except Exception:
        pass


def elide(txt, width=terminal_width()):
    """Elide the provided string

    The string is elided to the specified width, which defaults to the width of
    the terminal.

    Arguments:
        txt: the string to potentially elide
        width: the maximum permitted length of the string

    Returns:
        A string that is no longer than the specified width.

    """
    if width is not None and width > 3:
        try:
            if len(txt) > width:
                return txt[:width - 3] + '...'
        except Exception:
            pass

    return txt


def alternative_editor(default):
    """Return the path to the 'alternatives' editor

    Argument:
        default: the default to use if the alternatives editor cannot be found.

    """
    if os.path.exists('/usr/bin/editor'):
        return '/usr/bin/editor'

    return default


def environment_editor(default):
    """Return the user specified environment default

    Argument:
        default: the default to use if the environment variable contains
                nothing useful.

    """
    editor = os.environ.get('EDITOR', '').strip()
    if editor != '':
        return editor

    return default


def configuration_editor(config, default):
    """Return the editor in the config file

    Argument:
        default: the default to use if there is no editor in the config

    """
    try:
        return config.get('gist', 'editor')
    except configparser.NoOptionError:
        return default


def alternative_config(default):
    """Return the path to the config file in .config directory

    Argument:
        default: the default to use if ~/.config/gist does not exist.

    """
    config_path = os.path.expanduser(os.sep.join(['~', '.config', 'gist']))
    if os.path.isfile(config_path):
        return config_path
    else:
        return default


def xdg_data_config(default):
    """Return the path to the config file in XDG user config directory

    Argument:
        default: the default to use if either the XDG_DATA_HOME environment is
            not set, or the XDG_DATA_HOME directory does not contain a 'gist'
            file.

    """
    config = os.environ.get('XDG_DATA_HOME', '').strip()
    if config != '':
        config_path = os.path.join(config, 'gist')
        if os.path.isfile(config_path):
            return config_path

    return default


def main(argv=sys.argv[1:], config=None):
    args = docopt.docopt(
            __doc__,
            argv=argv,
            version='gist-v{}'.format(gist.__version__),
            )

    # Setup logging
    fmt = "%(created).3f %(levelname)s[%(name)s] %(message)s"
    logging.basicConfig(format=fmt)

    # Read in the configuration file
    if config is None:
        config = configparser.ConfigParser()
        config_path = os.path.expanduser(os.sep.join(['~', '.gist']))
        config_path = alternative_config(config_path)
        config_path = xdg_data_config(config_path)
        try:
            with open(config_path) as fp:
                config.read_file(fp)
        except Exception as e:
            message = 'Unable to load configuration file: {0}'.format(e)
            raise ValueError(message)

    try:
        log_level = config.get('gist', 'log-level').upper()
        logging.getLogger('gist').setLevel(log_level)
    except Exception:
        logging.getLogger('gist').setLevel(logging.ERROR)

    # Determine the editor to use
    editor = None
    editor = alternative_editor(editor)
    editor = environment_editor(editor)
    editor = configuration_editor(config, editor)

    if editor is None:
        raise ValueError('Unable to find an editor.')

    token = config.get('gist', 'token')
    gapi = gist.GistAPI(token=token, editor=editor)

    if args['list']:
        logger.debug(u'action: list')
        gists = gapi.list()
        for info in gists:
            public = '+' if info.public else '-'
            desc = '' if info.desc is None else info.desc
            line = u'{} {} {}'.format(info.id, public, desc)
            try:
                print(elide(line))
            except UnicodeEncodeError:
                logger.error('unable to write gist {}'.format(info.id))
        return

    if args['info']:
        gist_id = args['<id>']
        logger.debug(u'action: info')
        logger.debug(u'action: - {}'.format(gist_id))
        info = gapi.info(gist_id)
        print(json.dumps(info, indent=2))
        return

    if args['edit']:
        gist_id = args['<id>']
        logger.debug(u'action: edit')
        logger.debug(u'action: - {}'.format(gist_id))
        gapi.edit(gist_id)
        return

    if args['description']:
        gist_id = args['<id>']
        description = args['<desc>']
        logger.debug(u'action: description')
        logger.debug(u'action: - {}'.format(gist_id))
        logger.debug(u'action: - {}'.format(description))
        gapi.description(gist_id, description)
        return

    if args['fork']:
        gist_id = args['<id>']
        logger.debug(u'action: fork')
        logger.debug(u'action: - {}'.format(gist_id))
        info = gapi.fork(gist_id)
        return

    if args['clone']:
        gist_id = args['<id>']
        gist_name = args['<name>']
        logger.debug(u'action: clone')
        logger.debug(u'action: - {} as {}'.format(gist_id, gist_name))
        gapi.clone(gist_id, gist_name)
        return

    if args['content']:
        gist_id = args['<id>']
        logger.debug(u'action: content')
        logger.debug(u'action: - {}'.format(gist_id))

        content = gapi.content(gist_id)
        gist_file = content.get(args['<filename>'])

        if args['--decrypt']:
            if not config.has_option('gist', 'gnupg-homedir'):
                raise GistError('gnupg-homedir missing from config file')

            homedir = config.get('gist', 'gnupg-homedir')
            logger.debug(u'action: - {}'.format(homedir))

            gpg = gnupg.GPG(gnupghome=homedir, use_agent=True)
            if gist_file is not None:
                print(gpg.decrypt(gist_file).data.decode('utf-8'))
            else:
                for name, lines in content.items():
                    lines = gpg.decrypt(lines).data.decode('utf-8')
                    print(u'{} (decrypted):\n{}\n'.format(name, lines))

        else:
            if gist_file is not None:
                print(gist_file)
            else:
                for name, lines in content.items():
                    print(u'{}:\n{}\n'.format(name, lines))

        return

    if args['files']:
        gist_id = args['<id>']
        logger.debug(u'action: files')
        logger.debug(u'action: - {}'.format(gist_id))
        for f in gapi.files(gist_id):
            print(f)
        return

    if args['archive']:
        gist_id = args['<id>']
        logger.debug(u'action: archive')
        logger.debug(u'action: - {}'.format(gist_id))
        gapi.archive(gist_id)
        return

    if args['delete']:
        gist_ids = args['<ids>']
        logger.debug(u'action: delete')
        for gist_id in gist_ids:
            logger.debug(u'action: - {}'.format(gist_id))
            gapi.delete(gist_id)
        return

    if args['version']:
        logger.debug(u'action: version')
        print('v{}'.format(gist.__version__))
        return

    if args['create']:
        logger.debug('action: create')

        # If encryption is selected, perform an initial check to make sure that
        # it is possible before processing any data.
        if args['--encrypt']:
            if not config.has_option('gist', 'gnupg-homedir'):
                raise GistError('gnupg-homedir missing from config file')

            if not config.has_option('gist', 'gnupg-fingerprint'):
                raise GistError('gnupg-fingerprint missing from config file')

        # Retrieve the data to add to the gist
        files = list()

        if sys.stdin.isatty():
            if args['FILES']:
                logger.debug('action: - reading from files')
                for path in args['FILES']:
                    name = os.path.basename(path)
                    with open(path, 'rb') as fp:
                        files.append(FileInfo(name, fp.read().decode('utf-8')))

            else:
                logger.debug('action: - reading from editor')
                filename = args.get("<filename>", "file1.txt")

                # Determine whether the temporary file should be deleted
                if config.has_option('gist', 'delete-tempfiles'):
                    delete = config.getboolean('gist', 'delete-tempfiles')
                else:
                    delete = True

                with tempfile.NamedTemporaryFile('wb+', delete=delete) as fp:
                    logger.debug('action: - created {}'.format(fp.name))
                    os.system('{} {}'.format(editor, fp.name))
                    fp.flush()
                    fp.seek(0)

                    files.append(FileInfo(filename, fp.read().decode('utf-8')))

                if delete:
                    logger.debug('action: - removed {}'.format(fp.name))

        else:
            logger.debug('action: - reading from stdin')
            filename = args.get("<filename>", "file1.txt")
            files.append(FileInfo(filename, sys.stdin.read()))

        # Ensure that there are no empty files
        for file in files:
            if len(file.content) == 0:
                raise GistError("'{}' is empty".format(file.name))

        description = args['<desc>']
        public = args['--public']

        # Encrypt the files or leave them unmodified
        if args['--encrypt']:
            logger.debug('action: - encrypting content')

            fingerprint = config.get('gist', 'gnupg-fingerprint')
            gnupghome = config.get('gist', 'gnupg-homedir')

            gpg = gnupg.GPG(gnupghome=gnupghome, use_agent=True)
            data = {}
            for file in files:
                cypher = gpg.encrypt(file.content.encode('utf-8'), fingerprint)
                content = cypher.data.decode('utf-8')

                data['{}.asc'.format(file.name)] = {'content': content}
        else:
            data = {file.name: {'content': file.content} for file in files}

        print(gapi.create(description, data, public))
        return


if __name__ == "__main__":
    try:
        main()
    except GistError as e:
        sys.stderr.write(u"GIST: {}\n".format(e.msg))
        sys.stderr.flush()
        sys.exit(1)
    except Exception as e:
        logger.error(str(e))
        sys.exit(1)
