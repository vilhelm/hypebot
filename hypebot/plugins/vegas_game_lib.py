# coding=utf-8
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
"""Rambling gambling games."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import abc
from collections import defaultdict
import math
import random
import re

from absl import logging
import arrow
from six import with_metaclass

from hypebot.core import inflect_lib
from hypebot.core import util_lib
from hypebot.plugins import inventory_lib
from hypebot.protos import bet_pb2


class GameBase(with_metaclass(abc.ABCMeta)):
  """Abstract class for a gambling game."""

  @abc.abstractproperty
  def name(self):
    """Unique name to identify gambling game."""

  @abc.abstractmethod
  def TakeBet(self, bet):
    """Determine if game wants to take the bet.

    Args:
      bet: The bet. May be modified in place if the game takes the bet.

    Returns:
      taken: boolean if game wants this bet.
    """

  @abc.abstractmethod
  def FormatBet(self, bet):
    """Return human readable string describing bet.

    Args:
      bet: dictionary describing bet.

    Returns:
      String describing bet.
    """

  @abc.abstractmethod
  def SettleBets(self, pool, msg_fn, *args, **kwargs):
    """Settles bets for game.

    Args:
      pool: bets keyed by user and then scope.
      msg_fn: {callable(channel, msg)} function to send messages.
      *args: defined by game.
      **kwargs: defined by game.

    Returns:
      winners: array of tuples (user, amount) of winners.
      unused_bets: dict of bets that were not processed.
    """


class StockGame(GameBase):
  """Betting on stock prices. Why don't you just invest in the market?"""

  def __init__(self, stocks):
    self._stocks = stocks

  @property
  def name(self):
    return 'stock'

  def TakeBet(self, bet):
    symbols = self._stocks.ParseSymbols(bet.target)
    if symbols and len(symbols) == 1:
      symbol = symbols[0].upper()
      quote = self._stocks.Quotes(symbols).get(symbol)
      if not quote:
        return
      bet.target = symbol
      stock_data = bet_pb2.StockData(quote=quote.price)
      bet.data.Pack(stock_data)
      return True

  def FormatBet(self, bet):
    stock_data = bet_pb2.StockData()
    bet.data.Unpack(stock_data)
    return '%s %s %s at $%0.2f' % (
        util_lib.FormatHypecoins(bet.amount),
        bet_pb2.Bet.Direction.Name(bet.direction).lower(),
        bet.target, stock_data.quote)

  def SettleBets(self, pool, msg_fn, *args, **kwargs):
    # Get quotes for each symbol that has a bet
    quote_set = {b.target for user_bets in pool.values() for b in user_bets}
    quotes = self._stocks.Quotes(list(quote_set))

    logging.info('Starting stock gamble, quotes: %s pool: %s', quotes, pool)
    pool_value = sum(x.amount for user_bets in pool.values() for x in user_bets)
    msg_fn(None, 'The trading day is closed! %s bet %s on stock' %
           (inflect_lib.Plural(len(pool), 'pleb'),
            util_lib.FormatHypecoins(pool_value)))

    winners = defaultdict(int)
    for user, users_bets in pool.items():
      winning_symbols = []
      losing_symbols = []
      net_amount = 0
      for bet in users_bets:
        stock_data = bet_pb2.StockData()
        bet.data.Unpack(stock_data)
        symbol = bet.target
        if not quotes.get(symbol):
          # No quote, take the money and whistle innocently.
          logging.info('Didn\'t get a quote for %s, ledger: %s', symbol, bet)
          continue
        cur_price = quotes[symbol].price
        prev_price = stock_data.quote

        bet_sign = -1 if bet.direction == bet_pb2.Bet.AGAINST else 1
        price_delta = cur_price - prev_price
        if bet_sign * price_delta > 0:
          bet_result = 'Won'
          # User won, default to 1:1 odds
          winnings = bet.amount * 2
          winning_symbols.append(symbol)
          net_amount += bet.amount
          winners[user] += winnings
        else:
          bet_result = 'Lost'
          losing_symbols.append(symbol)
          net_amount -= bet.amount
        if bet_result == 'Won':
          payout_str = ', payout %s' % util_lib.FormatHypecoins(winnings)
        else:
          payout_str = ''
        msg_fn(
            user,
            u'bet {bet[amount]} {bet[direction]} {bet[target]} at {price[prev]}'
            u', now at {price[cur]} => {0}{1}'.format(
                bet_result, payout_str,
                bet={
                    'amount': util_lib.FormatHypecoins(bet.amount),
                    'direction': bet_pb2.Bet.Direction.Name(
                        bet.direction).lower(),
                    'target': bet.target,
                },
                price={'cur': cur_price, 'prev': prev_price}))
        logging.info(
            u'GambleStock: %s %s %s at %s, now at %s => %s%s',
            user, bet.direction, symbol, prev_price, cur_price, bet_result,
            payout_str)
      right_snippet = _BuildSymbolSnippet(winning_symbols, 'right')
      wrong_snippet = _BuildSymbolSnippet(losing_symbols, 'wrong')

      if net_amount == 0:
        summary_snippet = 'breaking even for the day'
      else:
        summary_snippet = 'ending the day %s %s' % (
            'up' if net_amount > 0 else 'down',
            util_lib.FormatHypecoins(abs(net_amount)))

      user_message = '%s was %s%s%s' % (user, right_snippet, wrong_snippet,
                                        summary_snippet)
      if user in winners:
        msg_fn(user, ('You\'ve won %s thanks to your ability to predict the '
                      'whims of hedge fund managers!') %
               util_lib.FormatHypecoins(winners[user]))
      msg_fn(None, user_message)

    return (winners.items(), {})


class LCSGame(GameBase):
  """Betting on LCS matches."""

  def __init__(self, esports):
    self._esports = esports

  @property
  def name(self):
    return 'lcs'

  def _TimeBeforeMatchBlock(self, schedule, time):
    """Checks that the current time occurs before the match block happening at
    the given parameter 'time'. A match block is defined as a set of matches,
    each of which is less than 6 hours apart from its successor."""

    now = arrow.utcnow()
    if time <= now:
      return False
    block_start = time
    for match in reversed(schedule):
      if block_start.shift(hours=-6) < match['time'] < block_start:
        block_start = match['time']
    return now < block_start

  def TakeBet(self, bet):
    team_names = bet.target.split(' over ')
    teams = [self._esports.FindTeam(name) for name in team_names]
    if not teams or None in teams:
      return False
    if len(teams) > 2:
      return 'Only 2 teams play in a match silly.'
    teams = [t['acronym'] for t in teams]

    # Find next match for team(s) that hasn't started.
    schedule = self._esports.schedule
    for match in schedule:
      match_teams = (match['blue'], match['red'])
      if (self._TimeBeforeMatchBlock(schedule, match['time']) and
          not match.get('winner', None) and
          all([t in match_teams for t in teams])):
        # Determine predicted winner.
        if len(teams) == 1:
          teams.append(
              match['blue'] if teams[0] == match['red'] else match['red'])
        winner = teams[0] if bet.direction == bet_pb2.Bet.FOR else teams[1]
        loser = teams[1] if bet.direction == bet_pb2.Bet.FOR else teams[0]
        bet.target = match['id']
        lcs_data = bet_pb2.LCSData(winner=winner, loser=loser)
        bet.data.Pack(lcs_data)
        return True
    return 'No scheduled match for %s.' % ' and '.join(teams)

  def FormatBet(self, bet):
    lcs_data = bet_pb2.LCSData()
    bet.data.Unpack(lcs_data)
    return '%s for %s over %s' % (
        util_lib.FormatHypecoins(bet.amount),
        lcs_data.winner.upper(), lcs_data.loser.upper())

  def SettleBets(self, pool, msg_fn, *args, **kwargs):
    unused_bets = defaultdict(list)
    winners = defaultdict(int)
    pool_value = 0
    msgs = []

    for user, user_bets in pool.items():
      winning_teams = []
      losing_teams = []
      net_amount = 0
      for bet in user_bets:
        lcs_data = bet_pb2.LCSData()
        bet.data.Unpack(lcs_data)
        match = self._esports.matches.get(bet.target, {})
        logging.info('Game time: %s', match)
        winner = match.get('winner', None)
        if winner:
          logging.info('GambleLCS: %s bet %s for %s and %s won.', user,
                       bet.amount, lcs_data.winner, winner)
          pool_value += bet.amount
          if lcs_data.winner == winner:
            winning_teams.append(winner)
            winners[user] += bet.amount * 2
            net_amount += bet.amount
          else:
            losing_teams.append(lcs_data.winner)
            net_amount -= bet.amount
        else:
          logging.info('Unused bet: %s', bet)
          unused_bets[user].append(bet)

      if winning_teams or losing_teams:
        right_snippet = _BuildSymbolSnippet(winning_teams, 'right')
        wrong_snippet = _BuildSymbolSnippet(losing_teams, 'wrong')

        if net_amount == 0:
          summary_snippet = 'breaking even.'
        else:
          summary_snippet = 'ending %s %s.' % (
              'up' if net_amount > 0 else 'down',
              util_lib.FormatHypecoins(abs(net_amount)))

        user_message = '%s was %s%s%s' % (user, right_snippet, wrong_snippet,
                                          summary_snippet)
        msgs.append(user_message)

    if msgs:
      msg_fn(None, 'LCS match results in! %s bet %s.' %
             (inflect_lib.Plural(len(msgs), 'pleb'),
              util_lib.FormatHypecoins(pool_value)))
    for msg in msgs:
      msg_fn(None, msg)

    return (winners.items(), unused_bets)


class LotteryGame(GameBase):
  """Betting on the lottery! The more you bet, the greater your chance!"""

  # The amount that hypebot skims off the top of bets for hosting the lottery.
  _BOOKIE_PERCENT = 0.10
  # The maximum percent of the pot that one person may bet.
  _MAX_BET_PERCENT = 0.25

  def __init__(self, bookie):
    self._bookie = bookie
    self._winning_item = inventory_lib.Create('CoinPurse', None, None, {})

  @property
  def name(self):
    return 'lottery'

  def CapBet(self, user, amount, resolver):
    """Cap bet to a percent of the lottery."""
    pool = self._bookie.LookupBets(self.name, resolver=resolver)
    # Remove the user from the pool so their past bets don't have impact.
    if user in pool:
      del pool[user]
    jackpot, item = self.ComputeCurrentJackpot(pool)
    pool_value = jackpot + item.value
    max_bet = int((self._MAX_BET_PERCENT * pool_value) /
                  (1 - self._MAX_BET_PERCENT * (1 - self._BOOKIE_PERCENT)))
    return min(amount, max_bet)

  def TakeBet(self, bet):
    if re.match(r'(the )?(lottery|lotto|raffle|jackpot)', bet.target):
      bet.target = bet.resolver
      bet.amount = self.CapBet(bet.user, bet.amount, bet.resolver)
      return True
    return False

  def FormatBet(self, bet):
    return inflect_lib.Plural(bet.amount, '%s ticket' % self.name)

  def SettleBets(self, pool, msg_fn, *args, **kwargs):
    # The lotto is global, so there should only be a single bet for each user
    pool_value = sum(user_bets[0].amount for user_bets in pool.values())
    if not pool_value:
      return ([], {})
    msg_fn(None, 'All 7-11s are closed! %s bet %s on the lottery' %
           (inflect_lib.Plural(len(pool), 'pleb'),
            util_lib.FormatHypecoins(pool_value)))

    coins, item = self.ComputeCurrentJackpot(pool)
    winning_number = random.randint(1, pool_value)
    ticket_number = 0
    for user, user_bets in pool.items():
      num_tickets = user_bets[0].amount
      ticket_number += num_tickets
      if ticket_number >= winning_number:
        msg_fn(user, [
            'You\'ve won %s in the lottery!' %
            util_lib.FormatHypecoins(coins),
            ('We\'ve always been such close friends. Can I borrow some money '
             'for rent?')
        ])
        msg_fn(None, '%s won %s and a(n) %s in the lottery!' % (
            user, util_lib.FormatHypecoins(coins), item.human_name))
        return ([(user, coins), (user, item)], {})

    return ([], {})

  def ComputeCurrentJackpot(self, pool):
    pool_value = 0
    for user_bets in pool.values():
      pool_value += sum(bet.amount for bet in user_bets)
    return (int(math.floor(pool_value * (1 - self._BOOKIE_PERCENT))),
            self._winning_item)


# A small helper method for formatting the summary output
def _BuildSymbolSnippet(symbols, adj, display_max=4):
  if not symbols:
    return ''
  response_symbols = symbols[:display_max]
  if len(symbols) > display_max:
    response_symbols.pop()
    num_extras = len(symbols) - len(response_symbols)
    response_symbols.append(inflect_lib.Plural(num_extras, 'other'))
  if len(response_symbols) == 1:
    return '%s about %s; ' % (adj, response_symbols[0])
  return '%s about %s; ' % (
      adj,
      ' and '.join([', '.join(response_symbols[:-1]), response_symbols[-1]]))
