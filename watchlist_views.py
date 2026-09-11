"""Ephemeral Discord views used to remove personal watches.

The views deliberately receive a store object instead of importing the bot.
That keeps the UI usable by tests and avoids a circular import between the
Discord command registration and the persistence layer.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Iterable

import discord

if TYPE_CHECKING:
    from watchlist_bot import Watch


def _watch_label(watch: Watch) -> str:
    """Return a Discord-safe, compact label for a watch option."""
    return str(watch.item_name)[:100]


def _watch_description(watch: Watch) -> str:
    description = f"${float(watch.max_price):,.2f} max"
    if getattr(watch, "game", None):
        description += f" • {watch.game}"
    return description[:100]


class _OwnerView(discord.ui.View):
    """Base view that rejects interactions from everyone except its owner."""

    def __init__(self, owner_id: int, *, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.owner_id = int(owner_id)
        self._timed_out = False
        self._closed = False
        self._busy = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if getattr(getattr(interaction, "user", None), "id", None) != self.owner_id:
            await interaction.response.send_message(
                "This watch-removal menu belongs to someone else.",
                ephemeral=True,
            )
            return False
        if getattr(self, "_busy", False):
            await interaction.response.send_message(
                "Your removal request is already being processed.",
                ephemeral=True,
            )
            return False
        return True

    def _disable_items(self) -> None:
        for item in self.children:
            item.disabled = True

    async def _begin_destructive(self, interaction: discord.Interaction) -> bool:
        """Acknowledge a destructive component and serialize duplicate clicks."""
        if self._busy:
            await interaction.response.send_message(
                "A watch removal is already in progress.", ephemeral=True
            )
            return False
        self._busy = True
        try:
            await interaction.response.defer()
        except BaseException:
            self._busy = False
            raise
        return True

    async def on_timeout(self) -> None:
        # A timeout never writes to storage.  Disabling controls is best effort:
        # ephemeral messages can disappear before Discord accepts an edit.
        self._timed_out = True
        self._closed = True
        self._disable_items()
        message = getattr(self, "message", None)
        if message is not None:
            try:
                await message.edit(view=self)
            except (discord.HTTPException, discord.NotFound):
                pass


class _WatchSelect(discord.ui.Select):
    """A page-local select whose values are merged into the parent selection."""

    def __init__(self, view: "MultipleWatchRemovalView"):
        self.watch_view = view
        options = [
            discord.SelectOption(
                label=_watch_label(watch),
                value=str(watch.id),
                description=_watch_description(watch),
                default=watch.id in view.selected_ids,
            )
            for watch in view.current_page_watches
        ]
        super().__init__(
            placeholder="Choose watches to remove…",
            min_values=0,
            max_values=max(1, len(options)),
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.watch_view._select_page(interaction, self.values)


class MultipleWatchRemovalView(_OwnerView):
    """Paginated, owner-only multiselect for removing selected watches."""

    page_size = 25

    def __init__(
        self,
        store,
        owner_id: int,
        watches: Iterable[Watch],
        *,
        timeout: float = 300,
    ):
        self.store = store
        self.watches = list(watches)
        self.watch_by_id = {int(watch.id): watch for watch in self.watches}
        self.pages = [
            self.watches[offset:offset + self.page_size]
            for offset in range(0, len(self.watches), self.page_size)
        ] or [[]]
        self.page = 0
        self.selected_ids: set[int] = set()
        super().__init__(owner_id, timeout=timeout)
        self._install_page()

    @property
    def current_page_watches(self) -> list[Watch]:
        return self.pages[self.page]

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def content(self) -> str:
        selected = len(self.selected_ids)
        return (
            f"Select watches to remove (page {self.page + 1}/{self.page_count}). "
            f"{selected} selected. Choose **Remove selected** when ready."
        )

    def _install_page(self) -> None:
        self.clear_items()
        self.select = _WatchSelect(self)
        self.add_item(self.select)
        self.previous_button = _PreviousPageButton(self)
        self.next_button = _NextPageButton(self)
        self.remove_button = _RemoveSelectedButton(self)
        self.cancel_button = _CancelButton(self)
        self.previous_button.disabled = self.page == 0
        self.next_button.disabled = self.page >= self.page_count - 1
        self.remove_button.disabled = not self.selected_ids
        self.add_item(self.previous_button)
        self.add_item(self.next_button)
        self.add_item(self.remove_button)
        self.add_item(self.cancel_button)

    async def _edit(self, interaction: discord.Interaction) -> None:
        self._install_page()
        await interaction.response.edit_message(content=self.content(), view=self)

    async def _select_page(
        self, interaction: discord.Interaction, values: Iterable[str]
    ) -> None:
        if not await self.interaction_check(interaction):
            return
        if self._timed_out or self._closed:
            await interaction.response.send_message(
                "This watch-removal menu has expired.", ephemeral=True
            )
            return
        page_ids = {int(watch.id) for watch in self.current_page_watches}
        self.selected_ids.difference_update(page_ids)
        # Discord values originate from our options, but validating again keeps
        # forged/stale component payloads from deleting arbitrary row IDs.
        self.selected_ids.update(
            int(value) for value in values
            if str(value).isdigit() and int(value) in self.watch_by_id
        )
        await self._edit(interaction)

    async def _change_page(self, interaction: discord.Interaction, offset: int) -> None:
        if not await self.interaction_check(interaction):
            return
        if self._timed_out or self._closed:
            await interaction.response.send_message(
                "This watch-removal menu has expired.", ephemeral=True
            )
            return
        self.page = max(0, min(self.page_count - 1, self.page + offset))
        await self._edit(interaction)

    async def _remove_selected(self, interaction: discord.Interaction) -> None:
        if not await self.interaction_check(interaction):
            return
        if self._timed_out or self._closed:
            await interaction.response.send_message(
                "This watch-removal menu has expired.", ephemeral=True
            )
            return
        if not self.selected_ids:
            await interaction.response.send_message(
                "Choose at least one watch first.", ephemeral=True
            )
            return
        if not await self._begin_destructive(interaction):
            return
        selected_ids = tuple(self.selected_ids)
        expected_normalized = {
            watch_id: self.watch_by_id[watch_id].normalized_name
            for watch_id in selected_ids
            if watch_id in self.watch_by_id
        }
        try:
            deleted = await asyncio.to_thread(
                self.store.delete_watches,
                self.owner_id,
                selected_ids,
                expected_normalized,
            )
        except Exception:
            # The command handles database-specific errors before opening the
            # view; this final guard ensures a transient callback failure is
            # visible instead of silently acknowledging a destructive action.
            self._busy = False
            await interaction.edit_original_response(
                content="I couldn't remove those watches right now. Please try again.",
                view=self,
            )
            return
        self.stop()
        self._busy = False
        self._closed = True
        self._disable_items()
        if deleted:
            names = ", ".join(
                f"**{discord.utils.escape_markdown(str(name))}**"
                for name in deleted
            )
            message = f"Removed {len(deleted)} watch"
            message += "es" if len(deleted) != 1 else ""
            message += f": {names}."
        else:
            message = (
                "Those selections are no longer active, so nothing was removed."
            )
        await interaction.edit_original_response(content=message, view=self)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        if not await self.interaction_check(interaction):
            return
        if self._timed_out or self._closed:
            await interaction.response.send_message(
                "This watch-removal menu has expired.", ephemeral=True
            )
            return
        self.stop()
        self._closed = True
        self._disable_items()
        await interaction.response.edit_message(
            content="Canceled—your watches are unchanged.", view=self
        )


class _PreviousPageButton(discord.ui.Button):
    def __init__(self, view: MultipleWatchRemovalView):
        self.watch_view = view
        super().__init__(label="Previous", style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.watch_view._change_page(interaction, -1)


class _NextPageButton(discord.ui.Button):
    def __init__(self, view: MultipleWatchRemovalView):
        self.watch_view = view
        super().__init__(label="Next", style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.watch_view._change_page(interaction, 1)


class _RemoveSelectedButton(discord.ui.Button):
    def __init__(self, view: MultipleWatchRemovalView):
        self.watch_view = view
        super().__init__(
            label="Remove selected", style=discord.ButtonStyle.danger, row=1
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.watch_view._remove_selected(interaction)


class _CancelButton(discord.ui.Button):
    def __init__(self, view):
        self.watch_view = view
        super().__init__(label="Cancel", style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.watch_view._cancel(interaction)


class AllWatchRemovalConfirmationView(_OwnerView):
    """Owner-only confirmation that removes all watches current at confirm time."""

    def __init__(
        self,
        store,
        owner_id: int,
        count: int,
        *,
        timeout: float = 300,
    ):
        self.store = store
        self.count = int(count)
        super().__init__(owner_id, timeout=timeout)
        self.confirm_button = _ConfirmAllButton(self)
        self.cancel_button = _CancelButton(self)
        self.add_item(self.confirm_button)
        self.add_item(self.cancel_button)

    def content(self) -> str:
        return (
            f"You have **{self.count}** active watch"
            f"{'' if self.count == 1 else 'es'}. Remove all of them? "
            "This stops future alerts and clears queued, not-yet-sent alerts; "
            "delivered history is kept. This action cannot be undone."
        )

    async def _cancel(self, interaction: discord.Interaction) -> None:
        if not await self.interaction_check(interaction):
            return
        if self._timed_out or self._closed:
            await interaction.response.send_message(
                "This watch-removal menu has expired.", ephemeral=True
            )
            return
        self.stop()
        self._closed = True
        self._disable_items()
        await interaction.response.edit_message(
            content="Canceled—your watches are unchanged.", view=self
        )

    async def _confirm(self, interaction: discord.Interaction) -> None:
        if not await self.interaction_check(interaction):
            return
        if self._timed_out or self._closed:
            await interaction.response.send_message(
                "This watch-removal menu has expired.", ephemeral=True
            )
            return
        if not await self._begin_destructive(interaction):
            return
        try:
            deleted = await asyncio.to_thread(
                self.store.delete_all_watches, self.owner_id
            )
        except Exception:
            self._busy = False
            await interaction.edit_original_response(
                content="I couldn't remove your watches right now. Please try again.",
                view=self,
            )
            return
        self.stop()
        self._busy = False
        self._closed = True
        self._disable_items()
        await interaction.edit_original_response(
            content=(
                f"Removed all {len(deleted)} active watch"
                f"{'' if len(deleted) == 1 else 'es'}. "
                "Delivered history was kept; queued alerts were canceled."
            ),
            view=self,
        )


class _ConfirmAllButton(discord.ui.Button):
    def __init__(self, view: AllWatchRemovalConfirmationView):
        self.watch_view = view
        super().__init__(label="Remove all", style=discord.ButtonStyle.danger, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.watch_view._confirm(interaction)


