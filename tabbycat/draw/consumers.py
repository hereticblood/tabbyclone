import logging

from asgiref.sync import async_to_sync
from channels.generic.websocket import JsonWebsocketConsumer
from channels.layers import get_channel_layer
from django.utils.translation import gettext as _

from tournaments.mixins import RoundWebsocketMixin
from utils.mixins import SuperuserRequiredWebsocketMixin

from .models import Debate

logger = logging.getLogger(__name__)


class BaseAdjudicatorContainerConsumer(SuperuserRequiredWebsocketMixin, RoundWebsocketMixin, JsonWebsocketConsumer):
    """For receiving updates to either debates or preformed panels; making the
    supplied modifications; and re-broadcasting them. The intent is that the
    socket provides a dict of objects, which in turn have a dict of attributes
    that can be updated directly and the original object returned. This avoids
    having to serialise/re-serialise objects that creates many more queries"""

    def delete_adjudicators(self, container, adj_ids):
        return container.related_adjudicator_set.exclude(adjudicator_id__in=adj_ids).delete()

    def create_adjudicators(self, container, adj_id, adj_type):
        return container.related_adjudicator_set.update_or_create(
            adjudicator_id=adj_id, defaults={'type': adj_type})

    def update_adjudicators(self, container, adjudicators):
        # Delete adjudicators who aren't in the posted information
        adj_ids = [a["adjudicator"]["id"] for a in adjudicators]
        delete_count, deleted = self.delete_adjudicators(container, adj_ids)
        logger.debug("Deleted %d adjudicators from %s", delete_count, container)

        # Update or create positions of adjudicators in debate
        for adjudicator in adjudicators:
            adj_id = adjudicator['adjudicator']['id']
            adj_type = adjudicator['position']
            obj, created = self.create_adjudicators(container, adj_id, adj_type)

    def get_objects(self, ids):
        return self.model.objects.filter(id__in=ids)

    def receive_debates(self, json_objects, original_content):
        # Retrieve either the debates or panels
        debates_or_panels = self.get_objects(json_objects.keys())
        if debates_or_panels.count() == 0:
            self.send_error(_("TODO: error"), _("TODO: msg"))

        # Make and save the change to the objects based on the provided change
        # TODO: these should be a bulk update operation (F expression?) if they
        # are ever used to update a non trivial amount of objects
        for object_id, object_changes in json_objects.items():
            debate_or_panel = debates_or_panels.get(id=int(object_id))
            for attribute, value in object_changes.items():
                if attribute == "adjudicators":
                    # Manually handle setting debate or panel allocation
                    self.update_adjudicators(debate_or_panel, value)
                else:
                    setattr(debate_or_panel, attribute, value)
                    debate_or_panel.save()

        # Re-Broadcast initial payload to confirm the change to websockets
        async_to_sync(get_channel_layer().group_send)(
            self.group_name(), {
                'type': 'broadcast_debates_or_panels',
                'content': original_content
            }
        )

    def receive_action(self, action_function, user):

        # TODO: Make this selection mechanism more robust
        worker = "venues" if action_function == "allocate_debate_venues" else "adjallocation"

        async_to_sync(get_channel_layer().send)(worker, {
            "type": action_function, # Corresponds to the function
            "extra": {'user_id': user.id, 'round_id': self.round.id,
                      'tournament_id': self.tournament.id,
                      'group_name': self.group_name()}
        })

    def receive_json(self, content):
        # For convenience, allocation/Priorisation actions come over this socket
        # TODO: these should be async actions; await the response then send back
        if 'action' in content:
            self.receive_action(content['action'], self.scope["user"])
        else:
            self.receive_debates(content['debatesOrPanels'], content)

    def broadcast_debates_or_panels(self, event):
        self.send_json(event['content'])


class DebateEditConsumer(BaseAdjudicatorContainerConsumer):
    group_prefix = 'debates'
    model = Debate
