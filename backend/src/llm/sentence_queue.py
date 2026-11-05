import queue
import threading
from src.llm.sentence import Sentence
from src import utils

logger = utils.get_logger()


class SentenceQueue:
    __logging_level = 42
    __should_log = False

    def __init__(self) -> None:
        self.__queue: queue.Queue[Sentence] = queue.Queue[Sentence]()
        self.__get_lock: threading.Lock = threading.Lock()
        self.__put_lock: threading.Lock = threading.Lock()
        self.__is_more_to_come: bool = False
    
    @property
    def is_more_to_come(self) -> bool:
        return self.__is_more_to_come
    
    @is_more_to_come.setter
    def is_more_to_come(self, value: bool):
        self.__is_more_to_come = value

    # While waiting, how often to re-check is_more_to_come (a sentence still arrives immediately)
    _END_RECHECK_S: float = 0.1

    @utils.time_it
    def get_next_sentence(self) -> Sentence | None:
        self.log(f"Trying to aquire get_lock to get next sentence")
        with self.__get_lock:
            # Waits while the producer says more is coming, re-checking that flag: a terminator that was
            # queued and then cleared before this looked (resume_after_interrupting_action clears without
            # stopping generation) used to leave this blocked in get() forever once the producer declared the end
            while self.__queue.qsize() > 0 or self.__is_more_to_come:
                try:
                    retrieved_sentence = self.__queue.get(timeout=self._END_RECHECK_S)
                except queue.Empty:
                    continue
                self.log(f"Retrieved '{retrieved_sentence.text}'")
                return retrieved_sentence
            self.log(f"Nothing to get from queue, returning None")
            return None
    
    @utils.time_it
    def put(self, new_sentence: Sentence):
        self.log(f"Trying to aquire put_lock to put '{new_sentence.text}'")
        with self.__put_lock:
            self.log(f"Putting '{new_sentence.text}'")
            self.__queue.put(new_sentence)

    @utils.time_it
    def put_at_front(self, new_sentence: Sentence):
        self.log(f"Trying to aquire get_lock to put_at_front '{new_sentence.text}'")
        with self.__get_lock:
            self.log(f"Trying to aquire put_lock to put_at_front '{new_sentence.text}'")
            with self.__put_lock:            
                sentence_list: list[Sentence] = []
                try:
                    while True:
                        sentence_list.append(self.__queue.get_nowait() )
                except queue.Empty:
                    pass
                self.__queue.put_nowait(new_sentence)
                for s in sentence_list:
                    self.__queue.put_nowait(s)

    @utils.time_it
    def clear(self):
        self.log(f"Trying to aquire get_lock to clear()")
        with self.__get_lock:
            self.log(f"Trying to aquire put_lock to clear()")
            with self.__put_lock:
                try:
                    while True:                
                        self.__queue.get_nowait()
                except queue.Empty:
                    pass
    
    @utils.time_it
    def log(self, text: str):
        if(self.__should_log):
            logger.log(self.__logging_level, text)
