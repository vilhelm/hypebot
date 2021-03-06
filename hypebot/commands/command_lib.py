# Copyright 2018 The Hypebot Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Library for commands."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

from collections import defaultdict
from functools import partial
from functools import wraps
import random
import re
from threading import Lock
import time

from absl import logging
from typing import Any, Callable, Dict, List, Tuple, TYPE_CHECKING

from hypebot.core import params_lib
from hypebot.core import util_lib
from hypebot.data import messages
from hypebot.protos.channel_pb2 import Channel
from hypebot.protos.user_pb2 import User


class BaseCommand(object):
  """Base class for commands."""

  DEFAULT_PARAMS = params_lib.HypeParams({
      # See _Ratelimit for details.
      'ratelimit': {
          'enabled': True,
          # One of USER, GLOBAL, or CHANNEL. Calls from the same scope are
          # rate-limitted.
          'scope': 'USER',
          # Minimum number of seconds to pass between calls.
          'interval': 5,
          # Will only ratelimit if _Handle returns a value.
          'return_only': False,
      }
  })

  # Used to ignore a level of scoping.
  _DEFAULT_SCOPE = 'all'

  def __init__(self, params, core):
    self._params = params_lib.HypeParams(self.DEFAULT_PARAMS)
    self._params.Override(params)
    self._params.Lock()
    self.command_prefix = '%' if core.params.execution_mode.dev else '!'
    self._core = core
    self._parsers = []
    self._last_called = defaultdict(lambda: defaultdict(float))
    self._ratelimit_lock = Lock()
    self._spook_probs = []

  def Handle(self, channel: Channel, user: str, message: str) -> None:
    """Attempt to handle the message.

    Compare message against all parsers. If one of them accepts the message,
    send the parsed command to the internal _Handle function.

    Args:
      channel: Where to send the reply.
      user: User who invoked the command.
      message: Raw message from application.
    """
    for parser in self._parsers:
      take, args, kwargs = parser(channel, user, message)
      if take:
        if 'target_user' in kwargs:
          kwargs['target_user'] = self._ParseCommandTarget(
              user, kwargs['target_user'], message)

        response = self._Ratelimit(channel, user, *args, **kwargs)
        if response:
          self._Reply(channel, response)
        return  # Ensure we don't handle the same message twice.

  def _Ratelimit(self,
                 channel: Channel,
                 user: str,
                 *args, **kwargs):
    """Ratelimits calls/responses from Handling the message.

    In general this, prevents the same user, channel, or global triggering a
    command in quick succession. This works by timing calls and verifying that
    future calls have exceeded the interval before invocation.

    Some commands like to handle every message, but only respond to a few. E.g.,
    MissingPing needs every message to record the most recent user, but only
    sends a response when someone types '?'. In this case, we always execute the
    command and only ratelimit the response. The restriction being, that it is
    safe to call on every invocation. E.g., do not transfer hypecoins.

    Args:
      channel: Passed through to _Handle.
      user: Passed through to _Handle.
      *args: Passed through to _Handle.
      **kwargs: Passed through to _Handle.

    Returns:
      Optional message(s) to reply to the channel.
    """
    if (not self._params.ratelimit.enabled or
        channel.visibility == Channel.PRIVATE):
      return self._Handle(channel, user, *args, **kwargs)

    scoped_channel = Channel()
    scoped_channel.CopyFrom(channel)
    scoped_user = user
    if self._params.ratelimit.scope == 'GLOBAL':
      scoped_channel.id = self._DEFAULT_SCOPE
      scoped_user = self._DEFAULT_SCOPE
    elif self._params.ratelimit.scope == 'CHANNEL':
      scoped_user = self._DEFAULT_SCOPE

    with self._ratelimit_lock:
      t = time.time()
      delta_t = t - self._last_called[scoped_channel.id][scoped_user]
      response = None
      if self._params.ratelimit.return_only:
        response = self._Handle(channel, user, *args, **kwargs)
        if not response:
          return

      if delta_t < self._params.ratelimit.interval:
        logging.info('Call to %s._Handle ratelimited in %s for %s: %s < %s',
                     self.__class__.__name__, scoped_channel.id, scoped_user,
                     delta_t, self._params.ratelimit.interval)
        self._Reply(user, random.choice(messages.RATELIMIT_MEMES))
        return

      self._last_called[scoped_channel.id][scoped_user] = t
      return response or self._Handle(channel, user, *args, **kwargs)

  def _ParseCommandTarget(self, user: str, target_user: str,
                          message: str) -> User:
    """Processes raw target_user into a User class, resolving 'me' to user."""
    target_user = util_lib.BuildUser(target_user)
    # An empty target_user defaults to the calling user
    if target_user.id in ('', 'me'):
      self._core.last_command = partial(self.Handle, message=message)
      return util_lib.BuildUser(user, canonicalize=False)
    return target_user

  def _Reply(self, *args, **kwargs):
    return self._core.Reply(*args, **kwargs)

  def _Spook(self, user: str) -> None:
    """Creates a spooky encounter with user."""
    logging.info('Spooking %s', user)
    self._Reply(user, util_lib.GetWeightedChoice(messages.SPOOKY_STRINGS,
                                                 self._spook_probs))

  def _Handle(self,
              channel: Channel,
              user: str,
              *args, **kwargs):
    """Internal method that handles the command.

    *args and **kwargs are the logical arguments returned by the parsers.

    Args:
      channel: Where to send the reply.
      user: User who invoked the command.
      *args: defined by subclass
      **kwargs: defined by subclass
    Returns:
      Optional message(s) to reply to the channel.
    """
    pass


class BasePublicCommand(BaseCommand):
  """Same as BaseCommand, but defaults to no ratelimiter."""

  DEFAULT_PARAMS = params_lib.MergeParams(BaseCommand.DEFAULT_PARAMS,
                                          {'ratelimit': {
                                              'enabled': False
                                          }})


def _AddParserInInit(cls, parser: Callable, has_prefix: bool = False) -> None:
  original_init = cls.__init__
  def NewInit(self, *args, **kwargs):
    original_init(self, *args, **kwargs)
    bound_parser = parser
    if has_prefix:
      bound_parser = partial(parser, command_prefix=self.command_prefix)
    self._parsers.append(bound_parser)  # pylint: disable=protected-access

  cls.__init__ = NewInit


def _ParseArgs(match):
  """Returns a list of args and dict of kwargs based on match groups."""
  args = list(match.groups())
  kwargs = match.groupdict()
  for arg in kwargs.values():
    args.remove(arg)
  return args, kwargs


def CommandRegexParser(pattern: str,
                       flags: int = re.DOTALL,
                       reply_to_public: bool = True,
                       reply_to_private: bool = True):
  """Decorator to add a command-style regex parser to a class.

  A command is case insensitive and takes the form <prefix><regex><whitespace>
  where the prefix is optional when called from a private channel.

  Args:
    pattern: Regular expression to try to match against messages.
    flags: Regular expression flags.
    reply_to_public: Whether to respond to public channels or not.
    reply_to_private: Whether to respond to private channels or not.

  Returns:
    Decorator that adds parser to class.
  """

  def Parser(channel: Channel, unused_user: str, message: str,
             command_prefix: str) -> Tuple[bool, List, Dict]:
    """Determine if message should be handled.

    Args:
      channel: Where the message originated.
      unused_user: user name
      message: message
      command_prefix: required prefix to any command

    Returns:
      {tuple<boolean, *args, **kwargs} Whether to take message and parsed
        args/kwargs.
    """
    is_private = channel.visibility == Channel.PRIVATE
    # Cannot precompile since this depends on the channel.
    match = re.match(
        r'(?i)^%s%s%s\s*$' % (command_prefix, '?'
                              if is_private else '', pattern),
        message,
        flags=flags)
    if (match
        and (reply_to_private or not is_private)
        and (reply_to_public or is_private)):
      args, kwargs = _ParseArgs(match)
      return True, args, kwargs
    return False, [], {}

  def Decorator(cls):
    _AddParserInInit(cls, Parser, has_prefix=True)
    return cls

  return Decorator


def RegexParser(pattern):
  """Decorator to add a regex parser to a class.

  Match groups are returned as *args for command.

  Args:
    pattern: {string} Regular expression to try to match against messages.

  Returns:
    {callable} Decorator that adds parser to class.
  """
  regex = re.compile(pattern)

  def Parser(unused_channel: Channel,
             unused_user: str,
             message: str) -> Tuple[bool, List, Dict]:
    """Determine if message should be handled.

    Args:
      message: {string} message

    Returns:
      {tuple<boolean, *args, **kwargs} Whether to take message and parsed
        args/kwargs.
    """
    match = regex.search(message)
    if match:
      args, kwargs = _ParseArgs(match)
      return True, args, kwargs
    return False, [], {}

  def Decorator(cls):
    _AddParserInInit(cls, Parser)
    return cls

  return Decorator


def PublicParser(cls):
  """Parser that handles all public channels."""
  def Parser(channel: Channel,
             unused_user: str,
             message: str) -> Tuple[bool, List[Any], Dict]:
    if channel.visibility == Channel.PUBLIC:
      return True, [message], {}
    return False, [], {}

  _AddParserInInit(cls, Parser)
  return cls


# === Handler Decorators ===
# Put these decorators on _Handle to filter what is handled regardless of parser
# triggering. Useful for ratelimiting or channel specific behavior.
# TODO(someone): Make class decorators or conditional within command params. Left
# as similar to previous behavior for now.


def PrivateOnly(fn):
  """Decorator to restrict handling to queries."""
  @wraps(fn)
  def Wrapped(fn_self, channel: Channel, user: str, *args, **kwargs):
    if channel.visibility == Channel.PRIVATE:
      return fn(fn_self, channel, user, *args, **kwargs)
  return Wrapped


def LimitPublicLines(max_lines: int = 6):
  """Decorator factory to restrict lines returned to a public channel.

  When the command's response exceeds max_lines and targets a public
  channel, the response is re-routed to the calling user and a brief message is
  left in the target channel as an indication.

  Args:
    max_lines: Maximum number of lines to send to a public channel.
  Returns:
    Decorator.
  """

  def Decorator(fn):
    """Decorator to restrict lines returned to a public channel."""

    @wraps(fn)
    def Wrapped(fn_self, channel: Channel, user: str, *args, **kwargs):
      msg = fn(fn_self, channel, user, *args, **kwargs)
      if channel.visibility == Channel.PUBLIC and isinstance(
          msg, list) and len(msg) > max_lines:
        # TODO(someone): Switch to calling _core.interface.SendMessage.
        getattr(fn_self, '_core').Reply(user, msg)
        return u'It\'s long so I sent it privately.'
      return msg

    return Wrapped

  return Decorator


def DefaultMainChannelOnly(fn):
  """Decorator to only handle if default channel is a main channel."""
  @wraps(fn)
  def Wrapped(fn_self, *args, **kwargs):
    core_obj = getattr(fn_self, '_core')
    if IsMainChannel(core_obj.default_channel, core_obj.params.main_channels):
      return fn(fn_self, *args, **kwargs)
  return Wrapped


def IsMainChannel(channel, main_ids):
  for main_id in main_ids:
    if re.match(main_id, channel.id):
      return True
  return False


def MainChannelOnly(fn):
  """Decorator to restrict handling to main channel or private channels."""
  @wraps(fn)
  def Wrapped(fn_self, channel: Channel, user: str, *args, **kwargs):
    if (channel.visibility == Channel.PRIVATE or
        IsMainChannel(channel, getattr(fn_self, '_core').params.main_channels)):
      return fn(fn_self, channel, user, *args, **kwargs)
  return Wrapped


def HumansOnly(message='I\'m sorry %s, I\'m afraid I can\'t do that'):
  """Decorator to restrict handling to humans."""
  def Decorator(fn):
    @wraps(fn)
    def Wrapped(fn_self, channel: Channel, user: str, *args, **kwargs):
      if getattr(fn_self, '_core').user_tracker.IsBot(user):
        getattr(fn_self, '_Reply')(channel, message % user)
      else:
        return fn(fn_self, channel, user, *args, **kwargs)
    return Wrapped
  return Decorator


def RequireReady(obj_name):
  """Decorator which calls the decorated fn only if obj reports it is ready.

  Args:
    obj_name: The name of the object that must be ready for the call to fn to
        succeed. Note that if the decorated function's owning class (e.g. the
        first positional arg normally called `self`) does not have an object
        with this name, fn will never be called.
  Returns:
    Decorator.
  """

  def Decorator(fn):
    """Actual decorator to require an object is ready."""
    @wraps(fn)
    def Wrapped(fn_self, *args, **kwargs):
      """The wrapped version of fn called in place of fn."""
      obj = fn_self
      for attr in obj_name.split('.'):
        obj = getattr(obj, attr, None)
      if obj and obj.IsReady():
        return fn(fn_self, *args, **kwargs)

      try:
        channel = kwargs.get('channel') or args[0]
      except IndexError:
        # Not actually a _Handle function. Used for LCSGameCallback.
        return
      message_fn = getattr(fn_self, '_Reply')
      if message_fn:
        nick = getattr(fn_self, '_core').nick
        if not obj:
          message_fn(channel, '%s is not enabled for %s' % (obj_name, nick))
        else:
          message_fn(channel,
                     '%s.%s is still loading data, please try again later.' %
                     (nick, obj_name))

    return Wrapped
  return Decorator


class TextCommand(BaseCommand):
  """Randomly chooses a line from params.choices to say."""

  DEFAULT_PARAMS = params_lib.MergeParams(BaseCommand.DEFAULT_PARAMS, {
      'choices': ['Do not forget to override me.']
  })

  def __init__(self, *args):
    super(TextCommand, self).__init__(*args)
    self._choices = self._params.choices
    self._probs = []

  def _Handle(self, channel, user):
    if self._choices:
      choice = util_lib.GetWeightedChoice(self._choices, self._probs)
      if '{person}' in choice:
        choice = choice.format(choice, person=user)
      if choice.startswith('/me '):
        choice = '\x01ACTION %s\x01' % choice[4:]
      return choice

