from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404
from kombu.asynchronous.http import Response
from rest_framework import generics, response, status
from snailstamp.apps.tenant.models import Member
from snailstamp.apps.ledger.models import Content, Entry
from snailstamp.apps.ledger import services
from .serializers import CreateCollectionSerializer


class ListCreateInventoryView(generics.ListCreateAPIView):
    """Inventory is a collection without `act`.
    Think bought new pen, notebook, or a minted sticker.
    More like a `tools` to be used together to create new collections.
    Egg: writing journal with pen, adding stickers, and then publish a new collection of stories.
    """
    queryset = Content.objects.filter(status=Content.ContentStatus.ACTIVE)
    serializer_class = CreateCollectionSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        user_id = request.user.id
        entry_id = data.get('entry_id')
        entry = get_object_or_404(Entry, id=data.get('entry_id'))
        member = get_object_or_404(Member, user_id=user_id, organization_id=entry.issuer_id)
        actor_id = entry.issuer_id
        member_id = member.id

        # 1. Lahirkan (Mint) 1 Collection
        new_ids = services.mint_batch(
            entry_id=entry_id,
            issuer_id=actor_id,
            issuer_member_id=member_id,
            batch=1,
            prefix="ART-"
        )

        if not new_ids:
            return response.Response(
                {"detail": "Action failed. The supply entry might be full."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 2. Tambahkan teks artikel ke dalam Collection
        collection_id = new_ids[-1]
        content_id, log_seq = services.add_or_update_content(
            collection_id=collection_id,
            actor_id=actor_id,
            actor_member_id=member_id,
            title=data.get('title', ''),
            body=data['body'],
            format=data.get('format', 'plain'),
        )

        # return response.Response({
        #     "collection_id": collection_id,
        #     "content_id": content_id,
        #     "title": data.get('title'),
        #     "status": "created",
        #     "log_seq": log_seq
        # }, status=status.HTTP_201_CREATED)

        return response.Response(
            {
                "actor_id": actor_id,
                "member_id": member_id,
                # "content_id": content_id,
            },
            status=status.HTTP_201_CREATED
        )