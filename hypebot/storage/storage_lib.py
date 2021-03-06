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
"""Provides generic classes for interacting with persistent storage."""

# pylint: disable=broad-except

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import abc
import json

from absl import logging
from six import with_metaclass
import retrying
from typing import Any, Callable, List, Optional, Tuple, Union

from hypebot.types import HypeStr, JsonType
from hypebot.core import params_lib


class HypeTransaction(with_metaclass(abc.ABCMeta)):
  """A base class for transactions used by implementations of HypeStore.

  HypeTransactions are NOT thread-safe.
  """

  def __init__(self, tx_name: HypeStr):
    self._name = tx_name

  def __str__(self) -> HypeStr:
    return '[TX %s]' % self.name

  @property
  def name(self) -> HypeStr:
    return self._name

  @abc.abstractmethod
  def Commit(self) -> bool:
    """Commits this transaction to the backing store.

    Raises:
      CommitAbortException if the transaction committing raised an exception.
      This indicates that committing the transaction could be retried and might
      succeed.

    Returns:
      If the transaction was successfully committed or not. False indicates that
      something went wrong and the transaction will never be successfully
      committed.
    """


class HypeStore(with_metaclass(abc.ABCMeta)):
  """Abstract base class for HypeBot storage."""

  DEFAULT_PARAMS = params_lib.HypeParams()

  def __init__(self, params):
    self._params = params_lib.HypeParams(self.DEFAULT_PARAMS)
    self._params.Override(params)
    self._params.Lock()

  @abc.abstractproperty
  def engine(self):
    """The name of the backing engine. Should be all lowercase."""

  @abc.abstractmethod
  def GetValue(self, key: HypeStr, subkey: HypeStr,
               tx: Optional[HypeTransaction] = None) -> Optional[HypeStr]:
    """Fetch the value for key and subkey.

    Args:
      key: The primary key used to find the entry.
      subkey: The subkey used to find the entry.
      tx: Optional transaction to run the underlying storage call within.

    Raises:
      If an error occurs during fetching the value from the store. This will
      abort any transaction GetValue is called with.

    Returns:
      The value found, or None if no value is found.
    """

  @abc.abstractmethod
  def SetValue(self, key: HypeStr, subkey: HypeStr, value: Union[int, HypeStr],
               tx: Optional[HypeTransaction] = None) -> None:
    """Replaces the current value (if any) for key and subkey with value.

    Args:
      key: The primary key used to find the entry.
      subkey: The subkey used to find the entry.
      value: The new value to overwrite any existing value with.
      tx: Optional transaction to run the underlying storage call within.

    Raises:
      If an error occurs during setting the value from the store. This will
      abort any transaction SetValue is called with.

    Returns:
      None.
    """

  def UpdateValue(self, key: HypeStr, subkey: HypeStr, delta: int,
                  tx: Optional[HypeTransaction] = None) -> None:
    """Reads the current value for key/subkey and adds delta, atomically."""
    if tx:
      self._UpdateValue(key, subkey, delta, tx)
    else:
      self.RunInTransaction(self._UpdateValue, key, subkey, delta,
                            tx_name='%s/%s += %s' % (key, subkey, delta))

  def _UpdateValue(self, key: HypeStr, subkey: HypeStr, delta: int,
                   tx: HypeTransaction) -> None:
    """Internal version of UpdateValue which requires a transaction."""
    cur_value = self.GetValue(key, subkey, tx) or 0
    try:
      cur_value = int(cur_value)
    except Exception as e:
      logging.error(
          'Can\'t call UpdateValue on (%s, %s), value %s isn\'t an int [%s]',
          key, subkey, cur_value, type(cur_value))
      raise e
    new_value = cur_value + delta
    self.SetValue(key, subkey, new_value, tx)

  @abc.abstractmethod
  def GetSubkey(self, subkey: HypeStr,
                tx: Optional[HypeTransaction] = None) -> List[
                    Tuple[HypeStr, HypeStr]]:
    """Returns (key, value) tuples for all keys with a value for subkey."""

  def GetJsonValue(self, key: HypeStr, subkey: HypeStr,
                   tx: Optional[HypeTransaction] = None) -> (
                       Optional[JsonType]):
    """Gets and deserializes the JSON object for key and subkey."""
    value = None
    try:
      serialized_value = self.GetValue(key, subkey, tx)
      if serialized_value:
        value = json.loads(serialized_value)
      return value
    except Exception as e:
      logging.error('Error fetching JSON value for %s/%s:', key, subkey)
      raise e

  def SetJsonValue(self, key: HypeStr, subkey: HypeStr, json_value: JsonType,
                   tx: Optional[HypeTransaction] = None) -> None:
    """Serializes and stores json_value as a string."""
    try:
      value = json.dumps(json_value)
      self.SetValue(key, subkey, value, tx)
    except Exception as e:
      logging.error('Error storing JSON value for %s/%s.', key, subkey)
      # Re-raise so that transactions will be aborted.
      raise e

  def UpdateJson(self,
                 key: HypeStr,
                 subkey: HypeStr,
                 transform_fn: Callable[[JsonType], Any],
                 success_fn: Callable[[JsonType], bool],
                 is_set: bool = False,
                 tx: Optional[HypeTransaction] = None) -> bool:
    """Fetches a JSON object and stores it after applying transform_fn.

    Args:
      key: The storage key to operate on.
      subkey: The storage subkey to operate on.
      transform_fn: A function that accepts a deserialized JSON object (e.g.
        python dict or list) and modifies it in place. Return value is ignored.
      success_fn: A function that accepts a deserialized JSON object and returns
        a boolean, which is used as the final return value of UpdateJson. Note
        this function is applied to the deserialized object BEFORE transform_fn.
      is_set: If the JSON object should be treated as a set. This is required
        because JSON does not natively support sets, but a set can be easily
        represented by a list of members.
      tx: Optional transaction to include this update in. If no transaction is
        passed, a new transaction will be created to ensure the Update is
        atomic.

    Returns:
      The result of success_fn applied to the JSON object as it was when fetched
      from table.
    """
    if tx:
      return self._UpdateJson(key, subkey, transform_fn, success_fn, is_set, tx)
    tx_name = 'UpdateJson on %s/%s' % (key, subkey)
    return self.RunInTransaction(self._UpdateJson, key, subkey, transform_fn,
                                 success_fn, is_set, tx_name=tx_name)

  def _UpdateJson(self,
                  key: HypeStr,
                  subkey: HypeStr,
                  transform_fn: Callable[[JsonType], Any],
                  success_fn: Callable[[JsonType], bool],
                  is_set: bool,
                  tx: HypeTransaction) -> bool:
    """Internal version of UpdateJson, requiring a transaction."""
    raw_structure = self.GetJsonValue(key, subkey, tx) or {}
    if is_set:
      raw_structure = set(raw_structure)
    success = success_fn(raw_structure)
    transform_fn(raw_structure)
    if is_set:
      raw_structure = list(raw_structure)
    self.SetJsonValue(key, subkey, raw_structure, tx)
    return success

  @abc.abstractmethod
  def GetHistoricalValues(
      self,
      key: HypeStr,
      subkey: HypeStr,
      num_past_values: int,
      tx: Optional[HypeTransaction] = None) -> List[JsonType]:
    """Like GetJsonValue, but allows for getting past values for key/subkey.

    Args:
      key: The primary key used to find the entry.
      subkey: The subkey used to find the entry.
      num_past_values: The maximum number of historical values to return. If
        fewer values are found (including 0 values), only that many values will
        be returned.
      tx: Optional transaction to run the underlying storage call within.

    Raises:
      If an error occurs during fetching the values from the store. This will
      abort any transaction GetHistoricalValues is called with.

    Returns:
      A list of the values found in reverse chronological order (newest values
      first). When no values are found, returns an empty list.
    """

  @abc.abstractmethod
  def PrependValue(self,
                   key: HypeStr,
                   subkey: HypeStr,
                   new_value: JsonType,
                   max_length: Optional[int] = None,
                   tx: Optional[HypeTransaction] = None) -> None:
    """Like SetJsonValue, but keeps past values upto an optional max_length.

    Args:
      key: The primary key used to find the entry.
      subkey: The subkey used to find the entry.
      new_value: The new value to add to the entry.
      max_length: The maximum number of past values to keep. Note that not all
        storage engines will respect this value, so it may be set to None.
      tx: Optional transaction to run the underlying storage call within.

    Raises:
      If an error occurs during setting the value from the store. This will
      abort any transaction PrependValue is called with.

    Returns:
      None.
    """

  @abc.abstractmethod
  def NewTransaction(self, tx_name: HypeStr) -> HypeTransaction:
    """Returns a new concrete transaction.

    Args:
      tx_name: Short description for this transaction, used in logging to
        clarify what operations are attempted within this transaction.

    Returns:
      An implementation-specific subclass of HypeTransaction.
    """

  @retrying.retry(stop_max_attempt_number=6, wait_exponential_multiplier=200)
  def RunInTransaction(self, fn: Callable, *args, **kwargs) -> Any:
    """Retriably attemps to execute fn within a single transaction.

    The normal use of this function is as follows:

    store.RunInTransaction(self._DoWorkThatNeedsToBeAtomic, arg_1, arg_2)
    ...
    def _DoWorkThatNeedsToBeAtomic(self, arg_1, arg_2, tx=None):
      a = store.GetValue(arg_1, arg_2)
      a += '-foo'
      store.SetValue(arg_1, arg_2, a)

    With the above, _DoWorkThatNeedsToBeAtomic will be done inside a transaction
    and retried if committing the transaction fails for a storage-engine defined
    reason which means that retrying to commit the transaction might be
    successful.

    Args:
      fn: The function which is executed within a transaction. It must be safe
        to run multiple times in the case of a transaction abort (e.g.
        contention on one of the key/subkey pairs), and must accept the kwarg
        "tx", which is a HypeTransaction.
      *args: Positional arguments to pass to fn.
      **kwargs: Keyword arguments to pass to fn. Will always include "tx".

    Raises:
      If tx.Commit raises any exception other than CommitAbortException,
      this function will not retry, and re-raise that exception.

    Returns:
      The return value of fn if fn does not raise an Exception. If fn does
      raise, the transaction is aborted, RunInTransaction is not retried, and
      returns False.
    """
    tx = kwargs.get('tx')
    if not tx:
      tx_name = kwargs.pop('tx_name', '%s: %s %s' % (fn.__name__, args, kwargs))
      tx = self.NewTransaction(tx_name)
      kwargs['tx'] = tx
    try:
      return_value = fn(*args, **kwargs)
    except Exception:
      logging.exception('Transactional function %s threw, aborting %s:',
                        fn.__name__, tx)
      return False

    try:
      tx.Commit()
    except CommitAbortException as e:
      logging.warning('%s Commit failed! Retrying', tx)
      raise e
    except Exception:
      logging.exception('Unknown error, aborting %s:', tx)
      return False

    return return_value


class CommitAbortException(RuntimeError):
  """Exception indicating a transaction commit failed, but can be retried."""


class SyncedDict(dict):
  """A "synced" version of a dict which is persisted to a store.

  When a SyncedDict is constructed, it loads contents from the store, allowing
  users to  treat it like a normal dict, but have items persisted across process
  restarts. SyncedDict does not stay synchronized across concurrently running
  objects or processes. If you need to see the changes made from another copy
  with the same storage_key, you must first call Sync().
  """

  _POP_SENTINEL = object()
  _DEFAULT_SUBKEY = 'synced_object:'

  def __init__(self, store, storage_key, storage_subkey=None):
    super(SyncedDict, self).__init__()
    self._store = store
    self._storage_key = storage_key
    self._subkey = storage_subkey or self._DEFAULT_SUBKEY
    self.Sync()

  def __delitem__(self, key):
    super(SyncedDict, self).__delitem__(key)
    self._FlushToStorage()

  def __setitem__(self, key, value):
    super(SyncedDict, self).__setitem__(key, value)
    self._FlushToStorage()

  def pop(self, key, default=_POP_SENTINEL):
    # We use a sentinel here because pop() has different behavior when you pass
    # a default, even if that default is None.
    if default is self._POP_SENTINEL:
      super(SyncedDict, self).pop(key)
    else:
      super(SyncedDict, self).pop(key, default)
    self._FlushToStorage()

  def update(self, *args, **kwargs):
    super(SyncedDict, self).update(*args, **kwargs)
    self._FlushToStorage()

  def _FlushToStorage(self):
    self._store.SetJsonValue(self._storage_key, self._subkey,
                             super(SyncedDict, self).__self__)  # pytype: disable=attribute-error

  def Sync(self):
    """(Re)loads all data from the backing storage."""
    store = self._store.GetJsonValue(self._storage_key, self._subkey)
    self.clear()
    # Do not use SyncedDict.update since it will FlushToStorage unnecessarily.
    super(SyncedDict, self).update(store or {})
