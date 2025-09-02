from datetime import datetime, timedelta


class ThreadManager:
    """Manages threads with the help of Airtable"""

    def __init__(self, airtable_base):
        self._active_cache = {}
        self._completed_cache = {}
        self._message_mappings = {}
        self._thread_ts_to_user_id = {}
        self.active_threads_table = airtable_base.table("Active Threads")
        self.completed_threads_table = airtable_base.table("Completed Threads")

        self._load_from_airtable()

    def _load_from_airtable(self):
        """Load existing threads from Airtable"""
        try:
            # Load active threads
            active_records = self.active_threads_table.all()
            for record in active_records:
                fields = record["fields"]
                user_id = fields.get("user_id")
                if user_id:
                    self._active_cache[user_id] = {
                        "thread_ts": fields.get("thread_ts"),
                        "channel": fields.get("channel"),
                        "message_ts": fields.get("message_ts"),
                        "record_id": record["id"],
                        "last_activity": datetime.now()
                    }
                    if fields.get("thread_ts"):
                        self._thread_ts_to_user_id[fields.get("thread_ts")] = user_id

            # Load completed threads
            completed_records = self.completed_threads_table.all()
            for record in completed_records:
                fields = record["fields"]
                user_id = fields.get("user_id")
                if user_id:
                    if user_id not in self._completed_cache:
                        self._completed_cache[user_id] = []
                    self._completed_cache[user_id].append({
                        "thread_ts": fields.get("thread_ts"),
                        "channel": fields.get("channel"),
                        "message_ts": fields.get("message_ts"),
                        "record_id": record["id"]
                    })
                    if fields.get("thread_ts"):
                        self._thread_ts_to_user_id[fields.get("thread_ts")] = user_id
            completed_threads_count = sum(len(threads) for threads in self._completed_cache.values())
            print(f"Loaded {len(self._active_cache)} active and {completed_threads_count} completed threads from db")

        except Exception as err:
            print(f"Error loading threads from Airtable: {err}")

    def _check_airtable_for_user(self, user_id):
        try:
            # Use Airtable's formula syntax for an exact match
            formula = f"{{user_id}} = '{user_id}'"
            records = self.active_threads_table.all(formula=formula, max_records=1)

            if not records:
                return None
            
            record = records[0]
            fields = record["fields"]
            thread_data = {
                "thread_ts": fields.get("thread_ts"),
                "channel": fields.get("channel"),
                "message_ts": fields.get("message_ts"),
                "record_id": record["id"],
                "last_activity": datetime.now()
            }
            
            self._active_cache[user_id] = thread_data
            if thread_data.get("thread_ts"):
                self._thread_ts_to_user_id[thread_data["thread_ts"]] = user_id
            
            print(f"Cache miss for {user_id}. Fetched and cached active thread from Airtable.")
            return thread_data

        except Exception as err:
            print(f"Error checking Airtable for user {user_id}: {err}")
            return None

    def get_active_thread(self, user_id):
        """Get active thread for a user, checking Airtable on cache miss."""
        thread = self._active_cache.get(user_id)
        if thread:
            return thread
        
        return self._check_airtable_for_user(user_id)

    def has_active_thread(self, user_id):
        if user_id in self._active_cache:
            return True
        
        return self._check_airtable_for_user(user_id) is not None

    def create_active_thread(self, user_id, channel, thread_ts, message_ts):
        """Create new active thread"""
        try:
            record = self.active_threads_table.create({
                "user_id": user_id,
                "thread_ts": thread_ts,
                "channel": channel,
                "message_ts": message_ts,
            })

            self._active_cache[user_id] = {
                "thread_ts": thread_ts,
                "channel": channel,
                "message_ts": message_ts,
                "record_id": record["id"],
                "last_activity": datetime.now()
            }
            self._thread_ts_to_user_id[thread_ts] = user_id

            if user_id not in self._completed_cache:
                self._completed_cache[user_id] = []

            print(f"Created active thread for user {user_id}")
            return True

        except Exception as err:
            print(f"Error creating active thread in db: {err}")
            return False

    def update_thread_activity(self, user_id):
        """Updated last activity ts for a thread, if cached"""
        if user_id not in self._active_cache:
            return

        current_time = datetime.now()
        self._active_cache[user_id]["last_activity"] = current_time

        try:
            record_id = self._active_cache[user_id]["record_id"]
            self.active_threads_table.update(record_id, {
                "funny_field": current_time.strftime("%m/%d/%Y, %H:%M:%S")
            })
        except Exception as err:
            print(f"Error updating thread activity ts: {err}")

    def complete_thread(self, user_id):
        """Mark active thread as completed"""
        if user_id not in self._active_cache:
            return False

        try:
            active_thread = self._active_cache[user_id]

            # Create the record for completed thread, delete the active one
            completed_record = self.completed_threads_table.create({
                "user_id": user_id,
                "thread_ts": active_thread["thread_ts"],
                "channel": active_thread["channel"],
                "message_ts": active_thread["message_ts"],
            })
            self.active_threads_table.delete(active_thread["record_id"])

            # Update cache
            if user_id not in self._completed_cache:
                self._completed_cache[user_id] = []
            self._completed_cache[user_id].append({
                "thread_ts": active_thread["thread_ts"],
                "channel": active_thread["channel"],
                "message_ts": active_thread["message_ts"],
                "record_id": completed_record["id"]
            })
            self._thread_ts_to_user_id[active_thread["thread_ts"]] = user_id
            del self._active_cache[user_id]

            print(f"Completed thread for user {user_id}")
            return True

        except Exception as err:
            print(f"Error completing thread: {err}")
            return False

    def get_completed_threads(self, user_id):
        """Get completed threads of a user"""
        return self._completed_cache.get(user_id, [])

    def delete_thread(self, user_id, message_ts):
        """Delete thread, either active or completed - doesn't matter"""
        try:
            # Try to delete an active thread with this ts if it exists
            if user_id in self._active_cache and self._active_cache[user_id]["message_ts"] == message_ts:
                record_id = self._active_cache[user_id]["record_id"]

                self.active_threads_table.delete(record_id)
                del self._active_cache[user_id]
                print(f"Deleted active thread for {user_id}")
                return self._active_cache.get(user_id)

            # Now look for completed thread with this ts, delete it if possible
            if user_id in self._completed_cache:
                for i, thread in enumerate(self._completed_cache[user_id]):
                    if thread["message_ts"] == message_ts:
                        record_id = thread["record_id"]

                        self.completed_threads_table.delete(record_id)
                        removed_thread = self._completed_cache[user_id].pop(i)
                        if removed_thread.get("thread_ts") in self._thread_ts_to_user_id:
                            del self._thread_ts_to_user_id[removed_thread.get("thread_ts")]
                        print(f"Deleted finished thread of {user_id}")
                        return removed_thread, False

            return None, False
        except Exception as err:
            print(f"Error deleting thread: {err}")
            return None, False

    @property
    def active_cache(self):
        return self._active_cache

    @property
    def completed_cache(self):
        return self._completed_cache
        
    def store_message_mapping(self, fraud_dept_ts, user_id, dm_ts, message_text, thread_ts):
        self._message_mappings[fraud_dept_ts] = {
            "user_id": user_id,
            "dm_ts": dm_ts,
            "message_text": message_text,
            "thread_ts": thread_ts
        }
        
    def get_message_mapping(self, fraud_dept_ts):
        """Get message mapping by fraud dept timestamp"""
        return self._message_mappings.get(fraud_dept_ts)
        
    def remove_message_mapping(self, fraud_dept_ts):
        """Remove message mapping"""
        if fraud_dept_ts in self._message_mappings:
            del self._message_mappings[fraud_dept_ts]
            
    def get_inactive_threads(self, hours=48):
        """Get threads inactive for more than specified hours"""
        cutoff_time = datetime.now() - timedelta(hours=hours)
        inactive_threads = []
        
        for user_id, thread_info in self._active_cache.items():
            last_activity = thread_info.get("last_activity", datetime.now())
            if last_activity < cutoff_time:
                inactive_threads.append({
                    "user_id": user_id,
                    "thread_info": thread_info,
                    "hours_inactive": (datetime.now() - last_activity).total_seconds() / 3600
                })
                
        return inactive_threads

    def get_user_by_thread_ts(self, thread_ts):
        return self._thread_ts_to_user_id.get(thread_ts)