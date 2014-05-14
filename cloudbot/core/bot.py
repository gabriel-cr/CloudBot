import asyncio
import time
import logging
import re
import os
import gc

from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.schema import MetaData

from .connection import BotConnection
from .config import Config
from .loader import PluginLoader
from .plugins import PluginManager
from .main import process
from ..util import botvars

logger_initialized = False


def clean_name(n):
    """strip all spaces and capitalization
    :type n: str
    :rtype: str
    """
    return re.sub('[^A-Za-z0-9_]+', '', n.replace(" ", "_"))


class CloudBot:
    """
    :type start_time: float
    :type running: bool
    :type do_restart: bool
    :type connections: list[cloudbot.core.connection.BotConnection]
    :type logger: logging.Logger
    :type data_dir: bytes
    :type config: core.config.Config
    :type plugin_manager: cloudbot.core.pluginmanager.PluginManager
    :type loader: cloudbot.core.loader.PluginLoader
    :type db_engine: sqlalchemy.engine.Engine
    :type db_factory: sqlalchemy.orm.session.sessionmaker
    :type db_session: sqlalchemy.orm.scoping.scoped_session
    :type db_metadata: sqlalchemy.sql.schema.MetaData
    :type handlers: dict[(str, str), core.main.Handler]
    """

    def __init__(self, loop=asyncio.get_event_loop()):
        # basic variables
        self.loop = loop
        self.start_time = time.time()
        self.running = True
        self.do_restart = False

        # stores all queued messages from all connections
        self.queued_messages = asyncio.Queue(loop=self.loop)
        # format: [{
        #   "conn": BotConnection, "raw": str, "prefix": str, "command": str, "params": str, "nick": str,
        #   "user": str, "host": str, "mask": str, "paramlist": list[str], "lastparam": str
        # }]

        # stores each bot server connection
        self.connections = []

        # set up logging
        self.logger = logging.getLogger("cloudbot")
        self.logger.debug("Logging system initialised.")

        # declare and create data folder
        self.data_dir = os.path.abspath('data')
        if not os.path.exists(self.data_dir):
            self.logger.debug("Data folder not found, creating.")
            os.mkdir(self.data_dir)

        # set up config
        self.config = Config(self)
        self.logger.debug("Config system initialised.")

        # setup db
        db_path = self.config.get('database', 'sqlite:///cloudbot.db')
        self.db_engine = create_engine(db_path)
        self.db_factory = sessionmaker(bind=self.db_engine)
        self.db_session = scoped_session(self.db_factory)
        self.db_metadata = MetaData()
        # set botvars.metadata so plugins can access when loading
        botvars.metadata = self.db_metadata
        self.logger.debug("Database system initialised.")

        # Bot initialisation complete
        self.logger.debug("Bot setup completed.")

        # Handlers
        self.singlethread_hook_futures = {}

        # create bot connections
        self.create_connections()

        self.loader = PluginLoader(self)
        self.plugin_manager = PluginManager(self)

    def run(self):
        """
        Starts CloudBot.
        This will first load plugins, then connect to IRC, then start the main loop for processing input.
        """
        self.loop.run_until_complete(self.main_loop())
        self.loop.close()

    @asyncio.coroutine
    def main_loop(self):
        # load plugins
        yield from self.plugin_manager.load_all(os.path.abspath("modules"))
        # if we we're stopped while loading plugins, cancel that and just stop
        if not self.running:
            return
        # start plugin reloader
        self.loader.start()
        # start connections
        yield from asyncio.gather(*[conn.connect() for conn in self.connections], loop=self.loop)
        # run a manual garbage collection cycle, to clean up any unused objects created during initialization
        gc.collect()
        # start main loop
        self.logger.info("Starting main loop")
        while self.running:
            # This function will wait until a new message is received.
            message = yield from self.queued_messages.get()

            if not self.running:
                # When the bot is stopped, StopIteration is put into the queue to make sure that
                # self.queued_messages.get() doesn't block this thread forever.
                # But we don't actually want to process that message, so if we're stopped, just exit.
                return

            # process the message
            asyncio.async(process(self, message), loop=self.loop)

    def create_connections(self):
        """ Create a BotConnection for all the networks defined in the config """
        for conf in self.config['connections']:
            # strip all spaces and capitalization from the connection name
            readable_name = conf['name']
            name = clean_name(readable_name)
            nick = conf['nick']
            server = conf['connection']['server']
            port = conf['connection'].get('port', 6667)

            self.connections.append(BotConnection(self, name, server, nick, config=conf,
                                                  port=port, logger=self.logger, channels=conf['channels'],
                                                  use_ssl=conf['connection'].get('ssl', False),
                                                  readable_name=readable_name))
            self.logger.debug("[{}] Created connection.".format(readable_name))

    def stop(self, reason=None):
        """quits all networks and shuts the bot down"""
        self.logger.info("Stopping bot.")

        self.logger.debug("Stopping config reloader.")
        self.config.stop()

        self.logger.debug("Stopping plugin loader.")
        self.loader.stop()

        for connection in self.connections:
            if not connection.connected:
                # Don't close a connection that hasn't connected
                continue
            self.logger.debug("[{}] Closing connection.".format(connection.readable_name))

            if reason:
                connection.cmd("QUIT", [reason])
            else:
                connection.cmd("QUIT")

            connection.stop()

        self.running = False
        # We need to make sure that the main loop actually exists after this method is called. This will ensure that the
        # blocking queued_messages.get() method is executed, then the method will stop without processing it because
        # self.running = False
        self.queued_messages.put_nowait(StopIteration)

    def restart(self, reason=None):
        """shuts the bot down and restarts it"""
        self.do_restart = True
        self.stop(reason)
