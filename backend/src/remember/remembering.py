from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from src.character_manager import Character
from src.characters_manager import Characters
from src.llm.message_thread import message_thread


@dataclass
class FinalSaveProgress:
    """What a conversation's final save has already persisted.

    A final save writes several records (each NPC's conversation log, each NPC's summary, shared summaries) and
    can fail part-way (disk full, file locked). It is retried by the next end, so every record written is noted
    here and a retry writes only the missing ones: never a duplicate, and never a second summary LLM call for an
    NPC whose summary was already generated. Each record is published atomically (complete new file or the old
    one, never a fragment), and a retry recognises a record whose publication landed although its attempt raised.
    """
    logs_written: set[str] = field(default_factory=set)
    summaries: dict[str, str] = field(default_factory=dict) # generated summary per NPC name
    summaries_written: set[str] = field(default_factory=set)
    shares_written: set[tuple[str, str, str]] = field(default_factory=set)
    pending_shares: list[tuple[str, str, str]] | None = None # captured once, by the first attempt
    # Digest of the full file content each record's attempt tried to publish (see src/atomic_file.py). The sets
    # above are only updated when a write returned; a write can land and still raise (os.replace, or anything
    # after it), so a retry compares the file with this digest before writing the record again
    publications: dict[object, str] = field(default_factory=dict)
    # Re-summarised text of a full summary file (keyed by its path), so a rollover that failed after the LLM call
    # is retried without a second call
    resummaries: dict[str, str] = field(default_factory=dict)


class Remembering(ABC):
    @abstractmethod
    def get_prompt_text(self, characters: list[Character], world_id: str) -> str:
        """ Generates a text that explains the previous interactions of the npcs with the player.
            Text is passed as part of the prompt to the LLM

        Args:
            characters (list[Character]): the non-player NPCs to load summaries for

        Returns:
            str: a single text
        """
        pass

    @abstractmethod
    def save_conversation_state(self, messages: message_thread, npcs_to_summarize: list[Character], npcs_in_conversation: Characters, world_id: str, is_reload=False, pending_shares: list[tuple[str, str, str]] | None = None, end_timestamp: float | None = None, is_radiant: bool = False, progress: FinalSaveProgress | None = None):
        """Saves the current state of the conversation.

        Args:
            messages (message_thread): The messages in the conversation
            npcs_to_summarize (list[Character]): The NPCs to generate summaries for
            npcs_in_conversation (Characters): All NPCs with participation history (for thread building and pending shares)
            world_id (str): The world ID for folder organization
            is_reload (bool): Whether this is a reload save
            pending_shares (list[tuple[str, str, str]] | None): List of (sharer_name, recipient_name, recipient_ref_id) for sharing summaries
            end_timestamp (float | None): Optional game timestamp to prepend to summaries
            is_radiant (bool): Whether this is a Radiant conversation (bypass min message threshold)
            progress (FinalSaveProgress | None): For a final save that may be a retry: records already written are
                skipped and every record written is added, so a failed attempt can be retried without duplicates
        """
        pass